from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from app.alarm.service import AlarmService
from app.notifier.main import NotifierStore, create_app as create_notifier_app


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    def __init__(self, app, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = port

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.02)
        raise RuntimeError("notifier test server failed to start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def notifier(tmp_path):
    store = NotifierStore(str(tmp_path / "notifier.db"))
    server = LiveServer(create_notifier_app(store), free_port())
    server.start()
    try:
        yield store, f"http://127.0.0.1:{server.port}"
    finally:
        server.stop()


@pytest.fixture()
def alarm(tmp_path, notifier, clock):
    _store, url = notifier
    service = AlarmService(
        tmp_path / "alarm.db",
        url,
        clock=clock,
        start_worker=False,
        retry_base=0.01,
    )
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def alarm_factory(tmp_path, notifier, clock):
    """Allows constructing a second AlarmService over the same DB (restart tests)."""
    _store, url = notifier
    services: list[AlarmService] = []

    def make() -> AlarmService:
        svc = AlarmService(
            tmp_path / "alarm.db",
            url,
            clock=clock,
            start_worker=False,
            retry_base=0.01,
        )
        services.append(svc)
        return svc

    yield make
    for svc in services:
        svc.close()
