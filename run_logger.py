"""
运行日志：记录每次工作流执行的内存 / 显存 / AI Core 曲线，以及每个节点的耗时和峰值。

原理：包装 PromptServer.send_sync，监听 ComfyUI 自身发出的
execution_start / executing / execution_success / execution_error / execution_interrupted 事件。
- 时间线（timeline）：执行期间每 ~0.5s 记录一行（npu-smi 设备显存、AI Core、温度、功耗，
  torch_npu 已分配/已保留显存，进程 RSS，系统/容器内存）。
- 节点（nodes）：每个实际执行的节点一行（耗时、torch_npu 峰值显存、设备显存峰值、
  AI Core 平均/峰值、进程内存峰值）。
文件写到 ComfyUI 的 output/npu_monitor_logs/ 下，保留最近 MAX_RUNS 次。
"""

import csv
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .npu_collector import MONITOR

log = logging.getLogger("NPUMonitor")

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

MAX_RUNS = int(os.environ.get("NPU_MONITOR_MAX_RUNS", "50"))
SAMPLE_INTERVAL = float(os.environ.get("NPU_MONITOR_LOG_INTERVAL", "0.5"))


def _log_dir() -> str:
    try:
        import folder_paths  # type: ignore

        base = folder_paths.get_output_directory()
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    d = os.path.join(base, "npu_monitor_logs")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- torch_npu
_torch = None


def _torch_npu():
    global _torch
    if _torch is None:
        try:
            import torch  # type: ignore
            import torch_npu  # type: ignore  # noqa: F401

            _torch = torch if torch.npu.is_available() else False
        except Exception:
            _torch = False
    return _torch or None


def _torch_mem() -> Dict[str, Optional[float]]:
    t = _torch_npu()
    if not t:
        return {"alloc_mb": None, "reserved_mb": None}
    try:
        return {
            "alloc_mb": t.npu.memory_allocated() / 1048576,
            "reserved_mb": t.npu.memory_reserved() / 1048576,
        }
    except Exception:
        return {"alloc_mb": None, "reserved_mb": None}


def _torch_reset_peak() -> None:
    t = _torch_npu()
    if t:
        try:
            t.npu.reset_peak_memory_stats()
        except Exception:
            pass


def _torch_peak() -> Dict[str, Optional[float]]:
    t = _torch_npu()
    if not t:
        return {"peak_alloc_mb": None, "peak_reserved_mb": None}
    try:
        return {
            "peak_alloc_mb": t.npu.max_memory_allocated() / 1048576,
            "peak_reserved_mb": t.npu.max_memory_reserved() / 1048576,
        }
    except Exception:
        return {"peak_alloc_mb": None, "peak_reserved_mb": None}


def _rss_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return psutil.Process().memory_info().rss / 1048576
    except Exception:
        return None


def _r(v, n=1):
    return None if v is None else round(v, n)


def _mx(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# ---------------------------------------------------------------- logger
class RunLogger:
    def __init__(self):
        self._lock = threading.Lock()
        self.run: Optional[Dict[str, Any]] = None
        self._node: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.enabled = os.environ.get("NPU_MONITOR_LOG", "1") != "0"

    # ---- event entry point (called from the execution thread) ----
    def on_event(self, event: str, data: Any) -> None:
        if not self.enabled or not isinstance(data, dict):
            return
        try:
            if event == "execution_start":
                self._start(data.get("prompt_id"))
            elif event == "execution_cached":
                if self.run is not None:
                    self.run["cached"] = len(data.get("nodes") or [])
            elif event == "executing":
                node = data.get("node")
                if node is None:
                    self._finish("success")
                    return
                if self.run is None:
                    self._start(data.get("prompt_id"))
                self._enter_node(str(node))
            elif event in ("execution_success", "execution_error", "execution_interrupted"):
                if self.run is not None:
                    status = event.replace("execution_", "")
                    if event == "execution_error":
                        self.run["error"] = str(data.get("exception_message", ""))[:500]
                    self._finish(status)
        except Exception:
            log.exception("NPU monitor run logger failed on %s", event)

    # ---- run lifecycle ----
    def _start(self, prompt_id: Optional[str]) -> None:
        if self.run is not None:
            self._finish("interrupted")
        prompt = self._lookup_prompt(prompt_id)
        self.run = {
            "prompt_id": prompt_id or "",
            "start": time.time(),
            "status": "running",
            "nodes": [],
            "timeline": [],
            "cached": 0,
            "_prompt": prompt,
        }
        self._node = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._sampler, name="NPUMonitorLog", daemon=True)
        self._thread.start()

    def _lookup_prompt(self, prompt_id) -> Dict[str, Any]:
        try:
            from server import PromptServer  # type: ignore

            q = PromptServer.instance.prompt_queue
            for item in list(getattr(q, "currently_running", {}).values()):
                if len(item) > 2 and item[1] == prompt_id:
                    return item[2] or {}
        except Exception:
            pass
        return {}

    def _enter_node(self, node_id: str) -> None:
        now = time.time()
        with self._lock:
            self._close_node(now)
            info = self.run["_prompt"].get(node_id) or {}
            self._node = {
                "node_id": node_id,
                "class_type": info.get("class_type", ""),
                "title": (info.get("_meta") or {}).get("title", ""),
                "start": now,
                "rss_start_mb": _rss_mb(),
                "alloc_start_mb": _torch_mem()["alloc_mb"],
                "dev_peak_mb": None,
                "rss_peak_mb": None,
                "aicore_sum": 0.0,
                "aicore_n": 0,
                "aicore_peak": None,
            }
        _torch_reset_peak()

    def _close_node(self, now: float) -> None:
        n = self._node
        if not n:
            return
        self._node = None
        peak = _torch_peak()
        rss = _rss_mb()
        self.run["nodes"].append(
            {
                "order": len(self.run["nodes"]) + 1,
                "node_id": n["node_id"],
                "class_type": n["class_type"],
                "title": n["title"],
                "start_s": _r(n["start"] - self.run["start"], 2),
                "duration_s": _r(now - n["start"], 3),
                "torch_peak_alloc_mb": _r(peak["peak_alloc_mb"]),
                "torch_peak_reserved_mb": _r(peak["peak_reserved_mb"]),
                "torch_alloc_delta_mb": _r(
                    (_torch_mem()["alloc_mb"] or 0) - (n["alloc_start_mb"] or 0)
                    if n["alloc_start_mb"] is not None
                    else None
                ),
                "device_peak_mb": _r(n["dev_peak_mb"]),
                "aicore_avg_pct": _r(n["aicore_sum"] / n["aicore_n"]) if n["aicore_n"] else None,
                "aicore_peak_pct": _r(n["aicore_peak"]),
                "rss_peak_mb": _r(_mx(n["rss_peak_mb"], rss)),
                "rss_delta_mb": _r(rss - n["rss_start_mb"]) if rss is not None and n["rss_start_mb"] is not None else None,
            }
        )

    def _finish(self, status: str) -> None:
        if self.run is None:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        now = time.time()
        with self._lock:
            self._close_node(now)
            run = self.run
            self.run = None
        run["status"] = status
        run["end"] = now
        run["duration_s"] = round(now - run["start"], 2)
        try:
            self._write(run)
        except Exception:
            log.exception("NPU monitor: failed to write run log")

    # ---- sampling thread ----
    def _sampler(self) -> None:
        last_ts = None
        while not self._stop.is_set():
            try:
                data = MONITOR.get()  # also keeps the npu-smi sampler alive
                tm = _torch_mem()
                rss = _rss_mb()
                with self._lock:
                    run, node = self.run, self._node
                    if run is None:
                        break
                    dev = (data.get("devices") or [{}])
                    fresh = data.get("ts") != last_ts
                    last_ts = data.get("ts")
                    used = sum(d.get("mem_used_mb") or 0 for d in dev) or None
                    total = sum(d.get("mem_total_mb") or 0 for d in dev) or None
                    aic = [d["aicore_pct"] for d in dev if d.get("aicore_pct") is not None]
                    aic_v = max(aic) if aic else None
                    sysd = data.get("system") or {}
                    row = {
                        "t_s": round(time.time() - run["start"], 2),
                        "node_id": node["node_id"] if node else "",
                        "class_type": node["class_type"] if node else "",
                        "device_used_mb": _r(used),
                        "device_total_mb": _r(total),
                        "aicore_pct": aic_v,
                        "temp_c": max([d["temp_c"] for d in dev if d.get("temp_c") is not None] or [None]) if dev else None,
                        "power_w": sum(d.get("power_w") or 0 for d in dev) or None,
                        "torch_alloc_mb": _r(tm["alloc_mb"]),
                        "torch_reserved_mb": _r(tm["reserved_mb"]),
                        "proc_rss_mb": _r(rss),
                        "ram_used_mb": _r(sysd.get("ram_used_mb")),
                        "ram_total_mb": _r(sysd.get("ram_total_mb")),
                        "cpu_pct": sysd.get("cpu_pct"),
                    }
                    run["timeline"].append(row)
                    if node:
                        node["rss_peak_mb"] = _mx(node["rss_peak_mb"], rss)
                        if fresh:
                            node["dev_peak_mb"] = _mx(node["dev_peak_mb"], used)
                            if aic_v is not None:
                                node["aicore_sum"] += aic_v
                                node["aicore_n"] += 1
                                node["aicore_peak"] = _mx(node["aicore_peak"], aic_v)
            except Exception:
                log.exception("NPU monitor log sampling failed")
            self._stop.wait(SAMPLE_INTERVAL)

    # ---- persistence ----
    def _write(self, run: Dict[str, Any]) -> None:
        d = _log_dir()
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(run["start"]))
        rid = f"{stamp}_{(run['prompt_id'] or 'run')[:8]}"
        tl, nodes = run["timeline"], run["nodes"]

        def col_max(key):
            vals = [r[key] for r in tl if r.get(key) is not None]
            return max(vals) if vals else None

        aic = [r["aicore_pct"] for r in tl if r.get("aicore_pct") is not None]
        summary = {
            "id": rid,
            "prompt_id": run["prompt_id"],
            "status": run["status"],
            "error": run.get("error"),
            "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["start"])),
            "duration_s": run["duration_s"],
            "executed_nodes": len(nodes),
            "cached_nodes": run.get("cached", 0),
            "peak_device_mb": col_max("device_used_mb"),
            "device_total_mb": col_max("device_total_mb"),
            "peak_torch_reserved_mb": max(
                [n["torch_peak_reserved_mb"] for n in nodes if n.get("torch_peak_reserved_mb") is not None] or [None]
            ),
            "peak_rss_mb": col_max("proc_rss_mb"),
            "peak_ram_mb": col_max("ram_used_mb"),
            "ram_total_mb": col_max("ram_total_mb"),
            "aicore_avg_pct": round(sum(aic) / len(aic), 1) if aic else None,
            "top_nodes": sorted(nodes, key=lambda n: n["duration_s"] or 0, reverse=True)[:10],
        }
        for name, rows in (("timeline", tl), ("nodes", nodes)):
            if not rows:
                continue
            with open(os.path.join(d, f"{rid}_{name}.csv"), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        with open(os.path.join(d, f"{rid}_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        self._prune(d)
        log.info(
            "NPU Monitor: run %s %s in %.1fs, peak device %.0f MB -> %s",
            rid, run["status"], run["duration_s"], summary["peak_device_mb"] or 0, d,
        )

    @staticmethod
    def _prune(d: str) -> None:
        ids = sorted({f.rsplit("_", 1)[0] for f in os.listdir(d) if f.endswith("_summary.json")})
        for old in ids[:-MAX_RUNS] if len(ids) > MAX_RUNS else []:
            for suffix in ("_summary.json", "_timeline.csv", "_nodes.csv"):
                try:
                    os.remove(os.path.join(d, old + suffix))
                except FileNotFoundError:
                    pass

    # ---- read API ----
    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        d = _log_dir()
        files = sorted((f for f in os.listdir(d) if f.endswith("_summary.json")), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    s = json.load(fh)
                s.pop("top_nodes", None)
                out.append(s)
            except Exception:
                pass
        return out

    def get_run(self, rid: str) -> Optional[Dict[str, Any]]:
        p = self.file_path(rid, "summary.json")
        if not p:
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def file_path(self, rid: str, suffix: str) -> Optional[str]:
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return None
        p = os.path.join(_log_dir(), f"{rid}_{suffix}")
        return p if os.path.isfile(p) else None


RUN_LOGGER = RunLogger()


def install_hook() -> bool:
    """Wrap PromptServer.send_sync so we see ComfyUI's execution events."""
    try:
        from server import PromptServer  # type: ignore

        srv = PromptServer.instance
        if getattr(srv.send_sync, "_npu_monitor_hooked", False):
            return True
        orig = srv.send_sync

        def send_sync(event, data, sid=None):
            RUN_LOGGER.on_event(event, data)
            return orig(event, data, sid)

        send_sync._npu_monitor_hooked = True  # type: ignore[attr-defined]
        srv.send_sync = send_sync
        return True
    except Exception as e:
        log.warning("NPU Monitor: run logging disabled (%s)", e)
        return False
