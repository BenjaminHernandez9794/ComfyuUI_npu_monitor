"""
ComfyUI NPU Monitor
- 悬浮、可自由拖动的状态栏：NPU 显存(HBM) / 系统内存 / AI Core 利用率 / 温度 / 功耗
- 节点 "NPU Monitor (Stats)"：在工作流里输出当前资源占用文本
"""

import logging

from aiohttp import web

from .npu_collector import MONITOR
from .run_logger import RUN_LOGGER, install_hook
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

log = logging.getLogger("NPUMonitor")

WEB_DIRECTORY = "./web"

try:
    from server import PromptServer

    routes = PromptServer.instance.routes

    @routes.get("/npu_monitor/stats")
    async def _npu_stats(request):
        return web.json_response(MONITOR.get())

    @routes.post("/npu_monitor/config")
    async def _npu_config(request):
        try:
            body = await request.json()
            if "interval" in body:
                MONITOR.set_interval(float(body["interval"]))
            return web.json_response({"ok": True, "interval": MONITOR.interval})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    install_hook()

    @routes.get("/npu_monitor/runs")
    async def _npu_runs(request):
        limit = int(request.query.get("limit", "20"))
        return web.json_response({"runs": RUN_LOGGER.list_runs(limit), "recording": RUN_LOGGER.run is not None})

    @routes.get("/npu_monitor/runs/{rid}")
    async def _npu_run(request):
        s = RUN_LOGGER.get_run(request.match_info["rid"])
        if s is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(s)

    @routes.get("/npu_monitor/runs/{rid}/{kind}.csv")
    async def _npu_run_csv(request):
        kind = request.match_info["kind"]
        if kind not in ("timeline", "nodes"):
            return web.json_response({"error": "bad kind"}, status=400)
        p = RUN_LOGGER.file_path(request.match_info["rid"], kind + ".csv")
        if not p:
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(
            p, headers={"Content-Disposition": f'attachment; filename="{request.match_info["rid"]}_{kind}.csv"'}
        )

except Exception as e:  # pragma: no cover - only when imported outside ComfyUI
    log.warning("NPU Monitor: PromptServer not available, routes not registered (%s)", e)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
