from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("notifier.main")

DB_PATH = os.getenv("NOTIFIER_DB", "/data/notifier.db")

# fault modes
#   normal            -> accept and return receipt
#   drop_receipt      -> persist + count as accepted, but respond 504 (receipt lost)
#   reject            -> do not persist, respond 503 (not accepted)
VALID_MODES = {"normal", "drop_receipt", "reject"}


class NotifyIn(BaseModel):
    notification_no: str
    device: str | None = None
    round_id: int | None = None
    triggered_at: float | None = None
    recovered_at: float | None = None
    maintenance_id: int | None = None
    message: str | None = None


class FaultIn(BaseModel):
    mode: str


class NotifierStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mode = "normal"
        with self._lock, self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_no TEXT PRIMARY KEY,
                    accepted_at REAL NOT NULL,
                    device TEXT,
                    round_id INTEGER,
                    triggered_at REAL,
                    recovered_at REAL,
                    maintenance_id INTEGER,
                    message TEXT,
                    deliveries INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            # Retired numbers are kept as tombstones only: the acceptance record
            # itself is deleted, but a cleaned number can never be issued again.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cleaned_notifications (
                    notification_no TEXT PRIMARY KEY,
                    round_id INTEGER NOT NULL,
                    cleaned_at REAL NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'mode'"
            ).fetchone()
            if row is not None and row["value"] in VALID_MODES:
                self._mode = str(row["value"])

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (mode,),
            )
            self._mode = mode
        return mode

    def accept(self, item: NotifyIn) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._db() as conn:
            tombstone = conn.execute(
                "SELECT round_id FROM cleaned_notifications WHERE notification_no = ?",
                (item.notification_no,),
            ).fetchone()
            if tombstone is not None:
                # A cleaned number is retired forever; a stale round must not be
                # resurrected on the notifier side either.
                return {
                    "accepted": False,
                    "conflict": "cleaned",
                    "detail": "该通知号已随轮次清理，不能再投递",
                }
            existing = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (item.notification_no,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["round_id"] is not None
                    and item.round_id is not None
                    and int(existing["round_id"]) != int(item.round_id)
                ):
                    # Same number, different round: never treat it as this round's receipt.
                    return {
                        "accepted": False,
                        "conflict": "round_mismatch",
                        "detail": "该通知号已属于另一轮，不能当作本轮受理",
                    }
                conn.execute(
                    "UPDATE notifications SET deliveries = deliveries + 1 WHERE notification_no = ?",
                    (item.notification_no,),
                )
                return {"accepted": True, "accepted_at": existing["accepted_at"], "duplicate": True}
            conn.execute(
                """
                INSERT INTO notifications
                    (notification_no, accepted_at, device, round_id, triggered_at,
                     recovered_at, maintenance_id, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.notification_no,
                    now,
                    item.device,
                    item.round_id,
                    item.triggered_at,
                    item.recovered_at,
                    item.maintenance_id,
                    item.message,
                ),
            )
            return {"accepted": True, "accepted_at": now, "duplicate": False}

    def clean(self, notification_no: str, round_id: int) -> dict[str, Any]:
        """Delete the acceptance record for exactly this round, idempotently.

        A number is only removed when its stored round_id matches. A tombstone
        keeps the retired number from ever being reused, and makes retries after
        a partial failure / notifier restart safe.
        """
        now = time.time()
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            if existing is not None:
                if existing["round_id"] is None or int(existing["round_id"]) != int(round_id):
                    return {
                        "deleted": False,
                        "reason": "round_mismatch",
                        "detail": "通知端同号记录不属于该轮，未删除",
                    }
                deleted = conn.execute(
                    "DELETE FROM notifications WHERE notification_no = ? AND round_id = ?",
                    (notification_no, round_id),
                ).rowcount
                conn.execute(
                    """
                    INSERT INTO cleaned_notifications(notification_no, round_id, cleaned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(notification_no) DO UPDATE SET
                        round_id = excluded.round_id,
                        cleaned_at = excluded.cleaned_at
                    """,
                    (notification_no, round_id, now),
                )
                return {"deleted": bool(deleted), "already_cleaned": False, "existed": True}
            tombstone = conn.execute(
                "SELECT round_id FROM cleaned_notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            if tombstone is not None:
                if int(tombstone["round_id"]) != int(round_id):
                    return {
                        "deleted": False,
                        "reason": "round_mismatch",
                        "detail": "该通知号已作为另一轮清理，不能当成本轮记录",
                    }
                return {"deleted": True, "already_cleaned": True, "existed": False}
            # Nothing was ever accepted for this round (skipped/cancelled/never sent).
            # Retire the number anyway so a retry is idempotent and it cannot be reused.
            conn.execute(
                "INSERT INTO cleaned_notifications(notification_no, round_id, cleaned_at) "
                "VALUES (?, ?, ?)",
                (notification_no, round_id, now),
            )
            return {"deleted": True, "already_cleaned": False, "existed": False}

    def lookup(self, notification_no: str) -> dict[str, Any] | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            return dict(row) if row else None

    def is_cleaned(self, notification_no: str) -> int | None:
        """Return the round_id that retired this number, if any."""
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT round_id FROM cleaned_notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            return int(row["round_id"]) if row is not None else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY accepted_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]


def create_app(store: NotifierStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.store is None:
            app.state.store = NotifierStore(DB_PATH)
            initial_mode = os.getenv("NOTIFIER_MODE", "normal")
            if initial_mode in VALID_MODES:
                app.state.store.set_mode(initial_mode)
        yield

    app = FastAPI(title="本地通知端（故障注入）", lifespan=lifespan)
    app.state.store = store

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "mode": app.state.store.mode}

    @app.post("/notify")
    def notify(item: NotifyIn, request: Request) -> JSONResponse:
        store: NotifierStore = request.app.state.store
        mode = store.mode
        if mode == "reject":
            logger.info("reject %s (fault mode)", item.notification_no)
            return JSONResponse(
                status_code=503,
                content={"accepted": False, "detail": "模拟通知端故障，未受理"},
            )
        if not item.notification_no.strip():
            return JSONResponse(status_code=422, content={"accepted": False})
        result = store.accept(item)
        if result.get("accepted") is not True:
            logger.info("conflict %s (%s)", item.notification_no, result.get("conflict"))
            return JSONResponse(status_code=409, content=result)
        if mode == "drop_receipt":
            logger.info("accepted %s then dropping receipt (fault mode)", item.notification_no)
            return JSONResponse(
                status_code=504,
                content={"accepted": True, "detail": "模拟已受理但回执丢失"},
            )
        logger.info("accepted %s", item.notification_no)
        return JSONResponse(status_code=200, content=result)

    @app.get("/notifications/{notification_no}")
    def lookup(notification_no: str, request: Request) -> JSONResponse:
        row = request.app.state.store.lookup(notification_no)
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"accepted": False, "detail": "通知端没有该通知号"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "accepted_at": row["accepted_at"],
                "notification_no": row["notification_no"],
                "deliveries": row["deliveries"],
            },
        )

    @app.get("/api/notifications")
    def list_notifications(request: Request) -> list[dict[str, Any]]:
        return request.app.state.store.recent()

    @app.post("/notifications/{notification_no}/clean")
    def clean(notification_no: str, request: Request, round_id: int) -> JSONResponse:
        """Remove this round's acceptance record; idempotent for safe retries."""
        result = request.app.state.store.clean(notification_no, round_id)
        status = 200 if result.get("deleted") else 409
        return JSONResponse(status_code=status, content=result)

    @app.get("/notifications/{notification_no}/cleaned")
    def cleaned(notification_no: str, request: Request) -> JSONResponse:
        round_id = request.app.state.store.is_cleaned(notification_no)
        return JSONResponse(
            status_code=200,
            content={"notification_no": notification_no, "cleaned": round_id is not None, "round_id": round_id},
        )

    @app.post("/api/fault")
    def set_fault(body: FaultIn, request: Request) -> dict[str, Any]:
        try:
            mode = request.app.state.store.set_mode(body.mode)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        return {"ok": True, "mode": mode}

    return app


app = create_app()
