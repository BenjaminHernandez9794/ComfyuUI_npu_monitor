"""
Ascend NPU / system resource collector.

Primary source : `npu-smi info` (shipped with the Ascend driver)
Fallback       : torch_npu (device memory only, no AI Core / temperature)
System         : psutil (RAM / CPU)

All sampling happens in a background thread so the ComfyUI event loop is
never blocked by the (fairly slow, ~0.3-1s) `npu-smi` call.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import psutil  # ComfyUI already depends on psutil
except Exception:  # pragma: no cover
    psutil = None

log = logging.getLogger("NPUMonitor")

_BUS_ID_RE = re.compile(r"^[0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-9A-Fa-f]$")
_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
# npu-smi parsing
# --------------------------------------------------------------------------- #
def _num(tok: Optional[str]) -> Optional[float]:
    if tok is None:
        return None
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None  # "NA", "-", ...


def _split_values(cell: str) -> List[Any]:
    """'0   1621 / 44280   3378/65536' -> ['0', (1621.0, 44280.0), (3378.0, 65536.0)]"""
    out: List[Any] = []
    pos = 0
    for m in _PAIR_RE.finditer(cell):
        out.extend(cell[pos:m.start()].split())
        out.append((float(m.group(1)), float(m.group(2))))
        pos = m.end()
    out.extend(cell[pos:].split())
    return out


def parse_npu_smi_info(text: str) -> List[Dict[str, Any]]:
    """
    Parse the main table of `npu-smi info`. Works for 910A / 910B / 310P
    (including multi-chip cards such as 310P Duo, where one NPU row is
    followed by several chip rows).

    Returns one dict per chip.
    """
    devices: List[Dict[str, Any]] = []
    npu_row: Optional[Dict[str, Any]] = None

    for raw in text.splitlines():
        line = raw.strip()
        if "Process id" in line or "Process name" in line:
            break  # process table starts, we are done
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        first, second, third = cells[0], cells[1], cells[2]
        if not first or not first[0].isdigit():
            continue  # header rows ("NPU Name", "Chip Bus-Id", ...)

        if _BUS_ID_RE.match(second):
            # ---- chip row: | chip [device] | bus-id | AICore  Mem a/b  [HBM a/b] |
            vals = _split_values(third)
            aicore = _num(vals[0]) if vals and not isinstance(vals[0], tuple) else None
            pairs = [v for v in vals if isinstance(v, tuple)]
            mem = pairs[0] if len(pairs) >= 1 else None
            hbm = pairs[1] if len(pairs) >= 2 else None
            # On 910 series "Memory-Usage" is usually 0/0 and the model lives
            # in HBM; on 310P the device memory *is* "Memory-Usage".
            dev_mem = hbm if (hbm and hbm[1] > 0) else mem
            chip_tokens = first.split()
            parent = npu_row or {}
            devices.append(
                {
                    "npu_id": parent.get("npu_id"),
                    "chip_id": int(chip_tokens[0]) if chip_tokens and chip_tokens[0].isdigit() else None,
                    "name": parent.get("name", "NPU"),
                    "health": parent.get("health"),
                    "bus_id": second,
                    "power_w": parent.get("power_w"),
                    "temp_c": parent.get("temp_c"),
                    "aicore_pct": aicore,
                    "mem_used_mb": dev_mem[0] if dev_mem else None,
                    "mem_total_mb": dev_mem[1] if dev_mem else None,
                    "mem_kind": "HBM" if dev_mem is hbm and hbm is not None else "DDR",
                    "source": "npu-smi",
                }
            )
        else:
            # ---- NPU row: | npu name | health | Power Temp Hugepages a/b |
            toks = first.split()
            vals = _split_values(third)
            scalars = [v for v in vals if not isinstance(v, tuple)]
            npu_row = {
                "npu_id": int(toks[0]) if toks[0].isdigit() else None,
                "name": " ".join(toks[1:]) or "NPU",
                "health": second,
                "power_w": _num(scalars[0]) if len(scalars) > 0 else None,
                "temp_c": _num(scalars[1]) if len(scalars) > 1 else None,
            }
    return devices


def find_npu_smi() -> Optional[str]:
    env = os.environ.get("NPU_SMI_PATH")
    if env and os.path.isfile(env):
        return env
    path = shutil.which("npu-smi")
    if path:
        return path
    for p in ("/usr/local/sbin/npu-smi", "/usr/local/bin/npu-smi", "/usr/sbin/npu-smi"):
        if os.path.isfile(p):
            return p
    return None


# --------------------------------------------------------------------------- #
# torch_npu fallback (memory only)
# --------------------------------------------------------------------------- #
def _torch_npu_devices() -> List[Dict[str, Any]]:
    try:
        import torch  # noqa: F401
        import torch_npu  # noqa: F401

        if not torch.npu.is_available():
            return []
        out = []
        for i in range(torch.npu.device_count()):
            free, total = torch.npu.mem_get_info(i)
            out.append(
                {
                    "npu_id": i,
                    "chip_id": 0,
                    "name": torch.npu.get_device_name(i),
                    "health": None,
                    "bus_id": None,
                    "power_w": None,
                    "temp_c": None,
                    "aicore_pct": None,
                    "mem_used_mb": (total - free) / 1024 / 1024,
                    "mem_total_mb": total / 1024 / 1024,
                    "mem_kind": "HBM",
                    "source": "torch_npu",
                }
            )
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# system stats
# --------------------------------------------------------------------------- #
def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v in ("max", "") else int(v)
    except Exception:
        return None


def _cgroup_memory() -> Optional[tuple]:
    """(used_bytes, limit_bytes) when running inside a memory-limited container."""
    # cgroup v2
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if limit:
        used = _read_int("/sys/fs/cgroup/memory.current")
        inactive = 0
        try:
            with open("/sys/fs/cgroup/memory.stat") as f:
                for line in f:
                    if line.startswith("inactive_file "):
                        inactive = int(line.split()[1])
        except Exception:
            pass
        if used is not None:
            return max(0, used - inactive), limit
    # cgroup v1
    limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if limit and limit < (1 << 60):
        used = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if used is not None:
            return used, limit
    return None


def _system_stats() -> Dict[str, Any]:
    if psutil is None:
        return {}
    vm = psutil.virtual_memory()
    used, total = vm.total - vm.available, vm.total
    cg = _cgroup_memory()
    if cg and 0 < cg[1] < vm.total:  # container limit is smaller than host RAM
        used, total = cg
    return {
        "ram_used_mb": used / 1024 / 1024,
        "ram_total_mb": total / 1024 / 1024,
        "ram_pct": round(100.0 * used / total, 1) if total else None,
        "cpu_pct": psutil.cpu_percent(interval=None),
    }


# --------------------------------------------------------------------------- #
# background sampler
# --------------------------------------------------------------------------- #
class NPUMonitor:
    """Lazy background sampler: only runs while a client polled recently."""

    IDLE_STOP_SECONDS = 15.0

    def __init__(self, interval: float = 1.0):
        self.interval = max(0.3, float(interval))
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"devices": [], "system": {}, "ts": 0, "error": None}
        self._thread: Optional[threading.Thread] = None
        self._last_request = 0.0
        self._smi = find_npu_smi()
        self._smi_failed = 0
        if psutil is not None:
            psutil.cpu_percent(interval=None)  # prime the counter

    # -- public ------------------------------------------------------------
    def set_interval(self, seconds: float) -> None:
        self.interval = min(60.0, max(0.3, float(seconds)))

    def get(self) -> Dict[str, Any]:
        self._last_request = time.time()
        self._ensure_thread()
        with self._lock:
            return dict(self._data, interval=self.interval)

    def sample_now(self) -> Dict[str, Any]:
        data = self._sample()
        with self._lock:
            self._data = data
        return data

    # -- internals ---------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="NPUMonitor", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while time.time() - self._last_request < self.IDLE_STOP_SECONDS:
            t0 = time.time()
            try:
                self.sample_now()
            except Exception as e:  # never let the thread die silently
                log.exception("NPU monitor sampling failed")
                with self._lock:
                    self._data["error"] = str(e)
            time.sleep(max(0.05, self.interval - (time.time() - t0)))

    def _run_npu_smi(self) -> Optional[str]:
        if not self._smi or self._smi_failed >= 5:
            return None
        try:
            r = subprocess.run(
                [self._smi, "info"], capture_output=True, text=True, timeout=10
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or f"exit code {r.returncode}")
            self._smi_failed = 0
            return r.stdout
        except Exception as e:
            self._smi_failed += 1
            log.warning("npu-smi failed (%s/5): %s", self._smi_failed, e)
            return None

    def _sample(self) -> Dict[str, Any]:
        devices: List[Dict[str, Any]] = []
        error = None
        out = self._run_npu_smi()
        if out:
            devices = parse_npu_smi_info(out)
        if not devices:
            devices = _torch_npu_devices()
        if not devices:
            error = "No Ascend NPU found (npu-smi / torch_npu unavailable)"
        for d in devices:
            if d.get("mem_used_mb") is not None and d.get("mem_total_mb"):
                d["mem_pct"] = round(100.0 * d["mem_used_mb"] / d["mem_total_mb"], 1)
            else:
                d["mem_pct"] = None
        return {"devices": devices, "system": _system_stats(), "ts": time.time(), "error": error}


MONITOR = NPUMonitor(interval=float(os.environ.get("NPU_MONITOR_INTERVAL", "1.0")))


def format_stats(data: Dict[str, Any]) -> str:
    lines = []
    for d in data.get("devices", []):
        tag = f"NPU{d.get('npu_id')}" + (f"-{d.get('chip_id')}" if d.get("chip_id") else "")
        parts = [f"{tag} {d.get('name')}"]
        if d.get("aicore_pct") is not None:
            parts.append(f"AICore {d['aicore_pct']:.0f}%")
        if d.get("mem_total_mb"):
            parts.append(
                f"{d.get('mem_kind')} {d['mem_used_mb']/1024:.1f}/{d['mem_total_mb']/1024:.1f}GB ({d['mem_pct']}%)"
            )
        if d.get("temp_c") is not None:
            parts.append(f"Temp {d['temp_c']:.0f}C")
        if d.get("power_w") is not None:
            parts.append(f"Power {d['power_w']:.1f}W")
        lines.append(" | ".join(parts))
    s = data.get("system") or {}
    if s:
        lines.append(
            f"RAM {s['ram_used_mb']/1024:.1f}/{s['ram_total_mb']/1024:.1f}GB ({s['ram_pct']}%) | CPU {s['cpu_pct']}%"
        )
    if data.get("error"):
        lines.append(data["error"])
    return "\n".join(lines)
