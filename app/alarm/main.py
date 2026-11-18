from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .service import AlarmBadRequest, AlarmConflict, AlarmService

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("alarm.main")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = os.getenv("ALARM_DB", "/data/alarm.db")
NOTIFIER_URL = os.getenv("NOTIFIER_URL", "http://notifier:8081")
NOTIFIER_TIMEOUT = float(os.getenv("NOTIFIER_TIMEOUT", "2.0"))


class MaintenanceIn(BaseModel):
    ends_at: float | None = None
    ends_in_minutes: float | None = None


def create_app(
    *,
    db_path: str | None = None,
    notifier_url: str | None = None,
    start_worker: bool = True,
    service: AlarmService | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.service = service
        else:
            app.state.service = AlarmService(
                db_path or DB_PATH,
                notifier_url or NOTIFIER_URL,
                notifier_timeout=NOTIFIER_TIMEOUT,
                start_worker=start_worker,
            )
        logger.info(
            "alarm service ready (db=%s notifier=%s)",
            db_path or DB_PATH,
            notifier_url or NOTIFIER_URL,
        )
        try:
            yield
        finally:
            app.state.service.close()

    app = FastAPI(title="供电柜告警检修抑制台", lifespan=lifespan)

    def svc(request: Request) -> AlarmService:
        return request.app.state.service

    @app.exception_handler(AlarmConflict)
    async def _conflict(_: Request, exc: AlarmConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AlarmBadRequest)
    async def _bad_request(_: Request, exc: AlarmBadRequest) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        return {"ok": True, "worker_running": svc(request).worker_running}

    @app.get("/api/state")
    def state(request: Request) -> dict[str, Any]:
        return svc(request).get_state()

    @app.post("/api/maintenance")
    def set_maintenance(body: MaintenanceIn, request: Request) -> dict[str, Any]:
        if body.ends_at is not None:
            ends_at = body.ends_at
        elif body.ends_in_minutes is not None:
            ends_at = time.time() + body.ends_in_minutes * 60
        else:
            raise AlarmBadRequest("需要提供 ends_at（Unix 秒）或 ends_in_minutes")
        if ends_at > 10_000_000_000:  # milliseconds accidentally supplied
            ends_at = ends_at / 1000.0
        return svc(request).set_maintenance(ends_at)

    @app.post("/api/maintenance/end")
    def end_maintenance(request: Request) -> dict[str, Any]:
        return svc(request).end_maintenance()

    @app.post("/api/alarm/trigger")
    def trigger(request: Request) -> dict[str, Any]:
        return svc(request).trigger()

    @app.post("/api/alarm/recover")
    def recover(request: Request) -> dict[str, Any]:
        return svc(request).recover()

    @app.post("/api/tick")
    def tick(request: Request) -> dict[str, Any]:
        svc(request).tick()
        return {"ok": True}

    @app.get("/api/notifier/observations")
    def notifier_observations(request: Request) -> dict[str, Any]:
        """Read-only view of what the local notifier actually accepted."""
        import httpx

        try:
            base = request.app.state.service._client.base_url
            items = httpx.get(f"{base}/api/notifications", timeout=2.0).json()
            mode = httpx.get(f"{base}/healthz", timeout=2.0).json().get("mode", "unknown")
        except Exception as exc:  # surfaced to the operator, not fatal
            raise HTTPException(status_code=502, detail=f"通知端不可达：{type(exc).__name__}") from exc
        return {"items": items, "mode": mode}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
