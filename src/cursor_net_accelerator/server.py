from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .cursor_fail import scan_recent_failures
from .service import ROUTING_MODES

if TYPE_CHECKING:
    from .service import AcceleratorService


def web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


def create_app(service: AcceleratorService) -> FastAPI:
    app = FastAPI(title="Cursor 网络加速器", version=__version__)
    static = web_dir()
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "hosts": list(service.settings.hosts),
        }

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse(service.status_payload())

    @app.get("/api/observe")
    def api_observe() -> JSONResponse:
        return JSONResponse(service.store.snapshot())

    @app.get("/api/pool")
    def api_pool() -> JSONResponse:
        return JSONResponse(service.pool.snapshot())

    @app.get("/api/health-timelines")
    def api_health_timelines(hours: float | None = None) -> JSONResponse:
        return JSONResponse(
            service.health.timelines(
                hours=hours,
                probe_interval=service.settings.probe_interval,
            )
        )

    @app.get("/api/cursor-failures")
    def api_cursor_failures(hours: float = 24.0) -> JSONResponse:
        return JSONResponse(scan_recent_failures(hours=hours))

    @app.post("/api/probe-now")
    def api_probe_now() -> JSONResponse:
        service.probe_once()
        return JSONResponse({"ok": True, "pool": service.pool.snapshot()})

    @app.post("/api/demo-seed-health")
    def api_demo_seed_health() -> JSONResponse:
        """Inject ~retention-hours of mixed strip data for UI review (clears prior history)."""
        host = service.settings.hosts[0] if service.settings.hosts else "api2.cursor.sh"
        # 角色顺序对齐监控器常见条：先红后绿 / 先绿后红 / 长期红
        preferred = ["3.92.129.183", "34.225.159.174", "52.21.130.202"]
        snap = service.pool.snapshot().get(host) or {}
        resolved = list(snap.get("resolved") or [])
        ips = [ip for ip in preferred if ip in resolved] or preferred
        for ip in resolved:
            if ip not in ips:
                ips.append(ip)
        ips = ips[:3]
        n = service.health.seed_demo(
            host,
            ips,
            hours=service.settings.health_retention_hours,
            interval=service.settings.probe_interval,
        )
        return JSONResponse(
            {
                "ok": True,
                "hostname": host,
                "ips": ips,
                "rounds": n,
                "retention_hours": service.settings.health_retention_hours,
                "health": service.health.timelines(
                    probe_interval=service.settings.probe_interval,
                ),
            }
        )

    @app.post("/api/routing-mode")
    async def api_routing_mode(request: Request) -> JSONResponse:
        body = await request.json()
        mode = (body or {}).get("mode")
        try:
            applied = service.set_routing_mode(str(mode))
        except ValueError as exc:
            allowed = [m for m in ROUTING_MODES if m != "bad" or service.settings.gate_test_enabled]
            return JSONResponse(
                {"ok": False, "error": str(exc), "allowed": allowed},
                status_code=400,
            )
        return JSONResponse(
            {
                "ok": True,
                "routing_mode": applied,
                "hint": (
                    "选路已切换。若 Cursor 仍异常，完全退出重开一次"
                    "（旧 TCP 连接不会因改 DNS 而断开）。"
                ),
            }
        )

    @app.get("/api/pac")
    def api_pac() -> Response:
        from .connect_proxy import generate_pac

        text = generate_pac(
            service.settings.hosts,
            service.settings.proxy_bind,
            service.settings.proxy_port,
        )
        return Response(content=text, media_type="application/x-ns-proxy-autoconfig")

    return app
