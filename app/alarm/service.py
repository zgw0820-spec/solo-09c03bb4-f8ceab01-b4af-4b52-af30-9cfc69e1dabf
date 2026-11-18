from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

logger = logging.getLogger(__name__)

DEVICE_NAME = "供电柜 #1"

STATE_LABELS = {
    "suppressed": "抑制中",
    "pending": "待送",
    "delivering": "待送（投递中）",
    "sent": "已送",
    "skipped": "未补送",
    "cancelled": "已取消",
}

GROUP_OF_STATE = {
    "suppressed": "suppressed",
    "pending": "pending",
    "delivering": "pending",
    "sent": "sent",
    "skipped": "skipped",
    "cancelled": "cancelled",
}


class AlarmConflict(Exception):
    """The requested operation conflicts with current alarm state."""


class AlarmBadRequest(Exception):
    """Invalid input for an alarm operation."""


class NotifyOutcome:
    def __init__(
        self,
        kind: str,
        accepted_at: float | None = None,
        detail: str = "",
        duplicate: bool = False,
    ) -> None:
        # kind: accepted | not_found | unknown
        self.kind = kind
        self.accepted_at = accepted_at
        self.detail = detail
        self.duplicate = duplicate


class NotifierClient:
    """HTTP client for the local notifier. Never guesses that a send succeeded."""

    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.timeout, connect=min(1.0, self.timeout))

    def notify(self, payload: dict[str, Any]) -> NotifyOutcome:
        try:
            resp = httpx.post(
                f"{self.base_url}/notify",
                json=payload,
                timeout=self._timeout(),
            )
        except httpx.HTTPError as exc:
            return NotifyOutcome("unknown", detail=f"通知请求失败（{type(exc).__name__}）")
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json()
            except ValueError:
                return NotifyOutcome("unknown", detail="通知端返回了无法解析的响应")
            if data.get("accepted") is True:
                accepted_at = data.get("accepted_at")
                if not isinstance(accepted_at, (int, float)):
                    accepted_at = None
                return NotifyOutcome(
                    "accepted",
                    accepted_at=float(accepted_at) if accepted_at is not None else None,
                    duplicate=bool(data.get("duplicate")),
                )
            return NotifyOutcome("unknown", detail="通知端未确认受理")
        return NotifyOutcome("unknown", detail=f"通知端 HTTP {resp.status_code}")

    def verify(self, notification_no: str) -> NotifyOutcome:
        try:
            resp = httpx.get(
                f"{self.base_url}/notifications/{notification_no}",
                timeout=self._timeout(),
            )
        except httpx.HTTPError as exc:
            return NotifyOutcome("unknown", detail=f"按通知号核实失败（{type(exc).__name__}）")
        if resp.status_code == 404:
            return NotifyOutcome("not_found", detail="通知端无此通知号")
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return NotifyOutcome("unknown", detail="核实响应无法解析")
            if data.get("accepted") is True:
                accepted_at = data.get("accepted_at")
                if not isinstance(accepted_at, (int, float)):
                    accepted_at = None
                return NotifyOutcome(
                    "accepted",
                    accepted_at=float(accepted_at) if accepted_at is not None else None,
                )
            return NotifyOutcome("not_found", detail="通知端记录未受理")
        return NotifyOutcome("unknown", detail=f"核实请求 HTTP {resp.status_code}")


class AlarmService:
    """Single-device alarm rounds with maintenance suppression and reliable delivery."""

    def __init__(
        self,
        db_path: str | Path,
        notifier_url: str,
        *,
        notifier_timeout: float = 2.0,
        clock: Callable[[], float] = time.time,
        retry_base: float = 0.5,
        retry_max: float = 8.0,
        worker_interval: float = 0.25,
        start_worker: bool = True,
    ) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.worker_interval = worker_interval
        self._lock = threading.RLock()
        self._client = NotifierClient(notifier_url, timeout=notifier_timeout)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_enabled = start_worker

        with self._lock:
            self._init_schema_locked()
            self._startup_recovery_locked()
        if start_worker:
            self.start_worker()

    # ---------- persistence helpers ----------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema_locked(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at REAL NOT NULL,
                    ends_at REAL NOT NULL,
                    closed_at REAL,
                    close_reason TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_no TEXT UNIQUE,
                    triggered_at REAL NOT NULL,
                    recovered_at REAL,
                    maintenance_id INTEGER REFERENCES maintenance_windows(id),
                    state TEXT NOT NULL,
                    scheduled_at REAL,
                    accepted_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    next_attempt_at REAL,
                    delivery_started_at REAL,
                    needs_verify INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    note TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alarm_rounds_state ON alarm_rounds(state)"
            )

    def _startup_recovery_locked(self) -> None:
        """A crash may leave rows in delivering: verify by number before re-sending."""
        now = self._clock()
        with self._db() as conn:
            cancelled = conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'cancelled',
                       needs_verify = 0,
                       next_attempt_at = NULL,
                       note = '服务重启时通知尚未发出，且告警已恢复，取消送达',
                       updated_at = ?
                 WHERE state = 'delivering'
                   AND recovered_at IS NOT NULL
                   AND delivery_started_at IS NULL
                """,
                (now,),
            ).rowcount
            changed = conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'pending',
                       needs_verify = 1,
                       next_attempt_at = ?,
                       note = '服务重启时投递结果未确认，先按通知号核实',
                       updated_at = ?
                 WHERE state = 'delivering'
                """,
                (now, now),
            ).rowcount
            if changed or cancelled:
                logger.info(
                    "startup recovery: %s in-flight row(s) to verify, %s cancelled",
                    changed,
                    cancelled,
                )
        self._expire_windows_locked(now)

    # ---------- state machine (call with lock held) ----------

    def _expire_windows_locked(self, now: float) -> None:
        with self._db() as conn:
            windows = conn.execute(
                "SELECT id FROM maintenance_windows WHERE closed_at IS NULL AND ends_at <= ?",
                (now,),
            ).fetchall()
        for window in windows:
            self._close_window_locked(int(window["id"]), "cutoff", now)

    def _close_window_locked(self, window_id: int, reason: str, now: float) -> None:
        with self._db() as conn:
            closed = conn.execute(
                """
                UPDATE maintenance_windows
                   SET closed_at = ?, close_reason = ?
                 WHERE id = ? AND closed_at IS NULL
                """,
                (now, reason, window_id),
            ).rowcount
            if not closed:
                return
            ends_at = float(
                conn.execute(
                    "SELECT ends_at FROM maintenance_windows WHERE id = ?", (window_id,)
                ).fetchone()["ends_at"]
            )
            rounds = conn.execute(
                """
                SELECT * FROM alarm_rounds
                 WHERE maintenance_id = ? AND state = 'suppressed'
                """,
                (window_id,),
            ).fetchall()
            reason_text = "检修截止" if reason == "cutoff" else "检修结束"
            for row in rounds:
                if row["recovered_at"] is not None:
                    conn.execute(
                        """
                        UPDATE alarm_rounds
                           SET state = 'skipped', updated_at = ?,
                               note = ?
                         WHERE id = ?
                        """,
                        (now, f"{reason_text}前已恢复，不补送通知", row["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE alarm_rounds
                           SET state = 'pending',
                               scheduled_at = ?,
                               next_attempt_at = ?,
                               updated_at = ?,
                               note = ?
                         WHERE id = ?
                        """,
                        (
                            ends_at,
                            now,
                            now,
                            f"{reason_text}时仍活跃，按固定通知号 {row['notification_no']} 送达",
                            row["id"],
                        ),
                    )

    def _claim_next_locked(self, now: float) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT * FROM alarm_rounds
                 WHERE state = 'pending'
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 ORDER BY id ASC
                 LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'delivering',
                       updated_at = ?
                 WHERE id = ? AND state = 'pending'
                """,
                (now, row["id"]),
            ).rowcount
            if not claimed:
                return None
            return dict(
                conn.execute("SELECT * FROM alarm_rounds WHERE id = ?", (row["id"],)).fetchone()
            )

    def _backoff(self, attempts: int) -> float:
        exponent = max(0, min(attempts - 1, 10))
        return min(self.retry_max, self.retry_base * (2**exponent))

    def _mark_sent(self, round_id: int, accepted_at: float | None, note: str) -> None:
        now = self._clock()
        with self._db() as conn:
            row = conn.execute(
                "SELECT recovered_at FROM alarm_rounds WHERE id = ?", (round_id,)
            ).fetchone()
            if row is not None and row["recovered_at"] is not None:
                note = f"{note}；告警已恢复，此通知在恢复前/投递中已受理，保留记录"
            conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'sent',
                       accepted_at = ?,
                       needs_verify = 0,
                       next_attempt_at = NULL,
                       last_error = NULL,
                       note = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (accepted_at if accepted_at is not None else now, note, now, round_id),
            )

    def _is_recovered(self, round_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT recovered_at FROM alarm_rounds WHERE id = ?", (round_id,)
            ).fetchone()
        return row is not None and row["recovered_at"] is not None

    def _cancel(self, round_id: int, note: str) -> None:
        now = self._clock()
        with self._db() as conn:
            conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'cancelled',
                       needs_verify = 0,
                       next_attempt_at = NULL,
                       note = ?,
                       updated_at = ?
                 WHERE id = ? AND state IN ('pending', 'delivering')
                """,
                (note, now, round_id),
            )

    def _retry(
        self,
        round_id: int,
        error: str,
        *,
        needs_verify: bool,
        note_prefix: str,
        definitive: bool = False,
    ) -> None:
        if self._is_recovered(round_id) and definitive:
            self._cancel(
                round_id,
                f"{note_prefix}；告警已恢复且通知未被受理，取消本轮的后续送达",
            )
            return
        now = self._clock()
        with self._db() as conn:
            row = conn.execute(
                "SELECT attempts, notification_no, recovered_at FROM alarm_rounds WHERE id = ?",
                (round_id,),
            ).fetchone()
            if row is None:
                return
            delay = self._backoff(int(row["attempts"]))
            if row["recovered_at"] is not None:
                note = (
                    f"{note_prefix}；告警已恢复，但投递结果未知，先按通知号 "
                    f"{row['notification_no']} 核实，不重复通知"
                )
            else:
                note = f"{note_prefix}；将按通知号 {row['notification_no']} 重试"
            conn.execute(
                """
                UPDATE alarm_rounds
                   SET state = 'pending',
                       needs_verify = ?,
                       next_attempt_at = ?,
                       last_error = ?,
                       note = ?,
                       updated_at = ?
                 WHERE id = ? AND state = 'delivering'
                """,
                (1 if needs_verify else 0, now + delay, error, note, now, round_id),
            )

    # ---------- delivery ----------

    def _perform_delivery(self, row: dict[str, Any]) -> None:
        round_id = int(row["id"])
        notification_no = str(row["notification_no"])

        # The network call happens outside the lock. Recovery may therefore race
        # with it: only a delivery that has actually started at the moment of
        # recovery is allowed to continue; one that never started is cancelled.
        now = self._clock()
        with self._lock, self._db() as conn:
            current = conn.execute(
                "SELECT state, recovered_at, delivery_started_at FROM alarm_rounds WHERE id = ?",
                (round_id,),
            ).fetchone()
            if current is None or current["state"] != "delivering":
                return
            if current["recovered_at"] is not None and not row["needs_verify"]:
                # A fresh notify is a delivery that has not started: recovery cancels it.
                self._cancel(round_id, "告警恢复时投递尚未开始，已取消送达")
                return
            # Every network attempt counts; delivery_started_at marks the first one,
            # which is what recovery uses to decide "cancelled" vs "keep reconciling".
            conn.execute(
                """
                UPDATE alarm_rounds
                   SET delivery_started_at = COALESCE(delivery_started_at, ?),
                       last_attempt_at = ?,
                       attempts = attempts + 1,
                       updated_at = ?
                 WHERE id = ?
                """,
                (now, now, now, round_id),
            )

        if row["needs_verify"]:
            outcome = self._client.verify(notification_no)
            if outcome.kind == "accepted":
                self._mark_sent(
                    round_id,
                    outcome.accepted_at,
                    "故障/重启后按通知号核实：通知端已受理，保留送达记录",
                )
                return
            if outcome.kind == "unknown":
                self._retry(
                    round_id,
                    outcome.detail or "核实结果未知",
                    needs_verify=True,
                    note_prefix="核实受理状态失败",
                )
                return
            if self._is_recovered(round_id):
                self._cancel(
                    round_id,
                    "告警已恢复，且按通知号核实通知端未受理；取消本轮送达",
                )
                return
            with self._db() as conn:
                conn.execute(
                    "UPDATE alarm_rounds SET needs_verify = 0 WHERE id = ?", (round_id,)
                )

        payload = {
            "notification_no": notification_no,
            "device": DEVICE_NAME,
            "round_id": round_id,
            "triggered_at": row["triggered_at"],
            "recovered_at": row["recovered_at"],
            "maintenance_id": row["maintenance_id"],
            "message": "供电柜过热告警",
        }
        outcome = self._client.notify(payload)
        if outcome.kind == "accepted":
            note = "通知端已受理"
            if outcome.duplicate:
                note = "通知端确认此前已按该通知号受理，未重复通知"
            self._mark_sent(round_id, outcome.accepted_at, note)
            return

        # Any non-confirmation is resolved by notification number, never assumed sent.
        verify = self._client.verify(notification_no)
        if verify.kind == "accepted":
            self._mark_sent(
                round_id,
                verify.accepted_at,
                f"回执未确认（{outcome.detail or '无回执'}），按通知号核实已受理，保留记录",
            )
            return
        if verify.kind == "unknown":
            self._retry(
                round_id,
                f"回执未确认（{outcome.detail or '无回执'}）；{verify.detail or '核实结果未知'}",
                needs_verify=True,
                note_prefix="投递结果未确认",
            )
            return

        if self._is_recovered(round_id):
            self._cancel(
                round_id,
                f"回执未确认（{outcome.detail or '无回执'}），按通知号核实未受理；"
                "告警已恢复，取消本轮送达",
            )
            return

        # Definitively not accepted: bounded backoff, same notification number.
        self._retry(
            round_id,
            outcome.detail or "通知端未受理",
            needs_verify=False,
            note_prefix="通知端未受理",
            definitive=True,
        )

    def tick(self) -> None:
        # Bounded so one tick can never spin forever; the worker calls this repeatedly.
        for _ in range(50):
            now = self._clock()
            with self._lock:
                self._expire_windows_locked(now)
                claimed = self._claim_next_locked(now)
            if claimed is None:
                return
            try:
                self._perform_delivery(claimed)
            except Exception:
                logger.exception("delivery round %s failed unexpectedly", claimed.get("id"))
                self._retry(
                    int(claimed["id"]),
                    "内部错误，投递未完成",
                    needs_verify=True,
                    note_prefix="投递异常",
                )

    # ---------- worker ----------

    def start_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="alarm-worker", daemon=True)
        self._thread.start()

    def _worker_loop(self) -> None:
        while not self._stop.wait(self.worker_interval):
            try:
                self.tick()
            except Exception:
                logger.exception("alarm worker tick failed")

    def stop_worker(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(2.0, self.worker_interval * 4))
        self._thread = None

    @property
    def worker_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- commands ----------

    def set_maintenance(self, cutoff_ts: float) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._expire_windows_locked(now)
            if cutoff_ts <= now:
                raise AlarmBadRequest("检修截止时刻必须晚于当前时间")
            with self._db() as conn:
                active = conn.execute(
                    """
                    SELECT * FROM maintenance_windows
                     WHERE closed_at IS NULL AND ends_at > ?
                     ORDER BY id DESC LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if active is not None:
                    conn.execute(
                        "UPDATE maintenance_windows SET ends_at = ? WHERE id = ?",
                        (cutoff_ts, active["id"]),
                    )
                    window_id = int(active["id"])
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO maintenance_windows
                            (started_at, ends_at, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (now, cutoff_ts, now),
                    )
                    window_id = int(cur.lastrowid)
                row = conn.execute(
                    "SELECT * FROM maintenance_windows WHERE id = ?", (window_id,)
                ).fetchone()
            return self._maintenance_dict(row, now)

    def end_maintenance(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._expire_windows_locked(now)
            with self._db() as conn:
                active = conn.execute(
                    """
                    SELECT * FROM maintenance_windows
                     WHERE closed_at IS NULL AND ends_at > ?
                     ORDER BY id DESC LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if active is None:
                    raise AlarmConflict("当前没有进行中的检修")
                window_id = int(active["id"])
                conn.execute(
                    "UPDATE maintenance_windows SET ends_at = ? WHERE id = ?",
                    (now, window_id),
                )
            self._close_window_locked(window_id, "manual", now)
            window = self._latest_window_locked()
            return self._maintenance_dict(window, now) if window else {}

    def trigger(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._expire_windows_locked(now)
            with self._db() as conn:
                active_round = conn.execute(
                    "SELECT * FROM alarm_rounds WHERE recovered_at IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if active_round is not None:
                    raise AlarmConflict(
                        f"第 {active_round['id']} 轮告警尚未恢复，不能重复触发"
                    )
                window = conn.execute(
                    """
                    SELECT * FROM maintenance_windows
                     WHERE closed_at IS NULL AND ends_at > ?
                     ORDER BY id DESC LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if window is not None:
                    state = "suppressed"
                    note = "检修中触发，暂缓通知；截止时仍活跃则按固定通知号送达"
                    scheduled_at = None
                    next_attempt_at = None
                else:
                    state = "pending"
                    note = "非检修时段触发，立即安排通知"
                    scheduled_at = now
                    next_attempt_at = now
                cur = conn.execute(
                    """
                    INSERT INTO alarm_rounds
                        (triggered_at, maintenance_id, state, scheduled_at,
                         next_attempt_at, note, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        int(window["id"]) if window is not None else None,
                        state,
                        scheduled_at,
                        next_attempt_at,
                        note,
                        now,
                    ),
                )
                round_id = int(cur.lastrowid)
                notification_no = f"ALM-{round_id:06d}"
                conn.execute(
                    "UPDATE alarm_rounds SET notification_no = ? WHERE id = ?",
                    (notification_no, round_id),
                )
                row = conn.execute(
                    "SELECT * FROM alarm_rounds WHERE id = ?", (round_id,)
                ).fetchone()
            return self._round_dict(row)

    def recover(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._expire_windows_locked(now)
            with self._db() as conn:
                row = conn.execute(
                    "SELECT * FROM alarm_rounds WHERE recovered_at IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    raise AlarmConflict("当前没有未恢复的告警")
                state = row["state"]
                note = row["note"]
                needs_verify = int(row["needs_verify"])
                if state == "suppressed":
                    # Recovered inside maintenance: never backfill this round.
                    state = "skipped"
                    note = "检修结束前恢复，不补送通知"
                elif state == "delivering" and row["delivery_started_at"] is None:
                    # Claimed but never sent: safe to cancel.
                    state = "cancelled"
                    needs_verify = 0
                    note = "告警恢复时投递尚未开始，已取消送达"
                elif state == "delivering":
                    # An attempt is already in flight: resolve by number, never re-send.
                    state = "pending"
                    needs_verify = 1
                    note = "告警恢复时投递已开始，只按原通知号确认结果，不再新发通知"
                elif state == "pending" and needs_verify:
                    note = "告警恢复时已有投递尝试，仅按原通知号核实结果，不再新发通知"
                elif state == "pending":
                    # Not started: cancellable.
                    state = "cancelled"
                    note = "告警恢复时投递尚未开始，已取消送达"
                elif state == "sent":
                    note = "告警恢复；此前通知已受理，保留送达记录"
                conn.execute(
                    """
                    UPDATE alarm_rounds
                       SET recovered_at = ?,
                           state = ?,
                           needs_verify = ?,
                           next_attempt_at = CASE
                               WHEN ? = 'cancelled' THEN NULL
                               WHEN ? = 'pending' THEN ?
                               ELSE next_attempt_at
                           END,
                           note = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (now, state, needs_verify, state, state, now, note, now, row["id"]),
                )
                updated = conn.execute(
                    "SELECT * FROM alarm_rounds WHERE id = ?", (row["id"],)
                ).fetchone()
            return self._round_dict(updated)

    # ---------- read models ----------

    def _latest_window_locked(self) -> sqlite3.Row | None:
        with self._db() as conn:
            return conn.execute(
                "SELECT * FROM maintenance_windows ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def _maintenance_dict(self, row: sqlite3.Row, now: float) -> dict[str, Any]:
        active = row["closed_at"] is None and row["ends_at"] > now
        return {
            "id": row["id"],
            "started_at": iso_z(row["started_at"]),
            "ends_at": iso_z(row["ends_at"]),
            "closed_at": iso_z(row["closed_at"]),
            "close_reason": row["close_reason"],
            "active": active,
            "remaining_seconds": max(0.0, float(row["ends_at"]) - now) if active else 0.0,
        }

    def _round_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        state = str(row["state"])
        return {
            "id": row["id"],
            "notification_no": row["notification_no"],
            "triggered_at": iso_z(row["triggered_at"]),
            "recovered_at": iso_z(row["recovered_at"]),
            "maintenance_id": row["maintenance_id"],
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "group": GROUP_OF_STATE.get(state, state),
            "is_active": row["recovered_at"] is None,
            "scheduled_at": iso_z(row["scheduled_at"]),
            "accepted_at": iso_z(row["accepted_at"]),
            "attempts": row["attempts"],
            "last_attempt_at": iso_z(row["last_attempt_at"]),
            "delivery_started_at": iso_z(row["delivery_started_at"]),
            "needs_verify": bool(row["needs_verify"]),
            "last_error": row["last_error"],
            "note": row["note"],
        }

    def get_state(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._expire_windows_locked(now)
            with self._db() as conn:
                rounds = conn.execute(
                    "SELECT * FROM alarm_rounds ORDER BY id DESC LIMIT 100"
                ).fetchall()
                active = conn.execute(
                    "SELECT * FROM alarm_rounds WHERE recovered_at IS NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                active_window = conn.execute(
                    """
                    SELECT * FROM maintenance_windows
                     WHERE closed_at IS NULL AND ends_at > ?
                     ORDER BY id DESC LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                latest_window = conn.execute(
                    "SELECT * FROM maintenance_windows ORDER BY id DESC LIMIT 1"
                ).fetchone()
                grouped = conn.execute(
                    """
                    SELECT state, COUNT(*) AS n FROM alarm_rounds GROUP BY state
                    """
                ).fetchall()
                totals = conn.execute(
                    "SELECT COUNT(*) AS total FROM alarm_rounds"
                ).fetchone()
            counts = {
                "suppressed": 0,
                "pending": 0,
                "sent": 0,
                "skipped": 0,
                "cancelled": 0,
            }
            for item in grouped:
                group = GROUP_OF_STATE.get(str(item["state"]))
                if group is not None:
                    counts[group] += int(item["n"])
            return {
                "device": DEVICE_NAME,
                "now": iso_z(now),
                "maintenance": self._maintenance_dict(active_window, now)
                if active_window is not None
                else None,
                "latest_maintenance": self._maintenance_dict(latest_window, now)
                if latest_window is not None
                else None,
                "active_round": self._round_dict(active) if active is not None else None,
                "rounds": [self._round_dict(row) for row in rounds],
                "counts": counts,
                "total_rounds": int(totals["total"]) if totals is not None else 0,
                "worker_running": self.worker_running,
            }

    def close(self) -> None:
        self.stop_worker()


def iso_z(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(float(ts), tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
