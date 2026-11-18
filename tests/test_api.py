from __future__ import annotations

import time

import httpx
from fastapi.testclient import TestClient

from app.alarm.main import create_app
from app.alarm.service import AlarmService


def make_client(tmp_path, notifier):
    _store, url = notifier
    service = AlarmService(
        tmp_path / "api-alarm.db",
        url,
        start_worker=False,
        retry_base=0.01,
    )
    app = create_app(service=service, db_path=str(tmp_path / "api-alarm.db"), notifier_url=url)
    return TestClient(app), service


def test_health_and_index_page(tmp_path, notifier):
    client, service = make_client(tmp_path, notifier)
    with client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        page = client.get("/")
        assert page.status_code == 200
        assert "供电柜告警检修抑制台" in page.text

        css = client.get("/static/app.css")
        assert css.status_code == 200
        js = client.get("/static/app.js")
        assert js.status_code == 200
    service.close()


def test_api_conflict_and_validation(tmp_path, notifier):
    client, service = make_client(tmp_path, notifier)
    with client:
        bad = client.post("/api/maintenance", json={"ends_at": time.time() - 10})
        assert bad.status_code == 400

        empty = client.post("/api/maintenance", json={})
        assert empty.status_code == 400

        no_recover = client.post("/api/alarm/recover")
        assert no_recover.status_code == 409

        created = client.post("/api/alarm/trigger").json()
        assert created["notification_no"].startswith("ALM-")
        duplicate = client.post("/api/alarm/trigger")
        assert duplicate.status_code == 409

        state = client.get("/api/state").json()
        assert state["active_round"]["id"] == created["id"]
        assert state["counts"]["pending"] == 1

        ended = client.post("/api/maintenance/end")
        assert ended.status_code == 409
    service.close()


def test_api_maintenance_ends_in_minutes(tmp_path, notifier, monkeypatch):
    client, service = make_client(tmp_path, notifier)
    with client:
        resp = client.post("/api/maintenance", json={"ends_in_minutes": 5})
        assert resp.status_code == 200
        window = resp.json()
        assert window["active"] is True
        assert 290 < window["remaining_seconds"] <= 300

        state = client.get("/api/state").json()
        assert state["maintenance"]["id"] == window["id"]

        created = client.post("/api/alarm/trigger").json()
        assert created["state"] == "suppressed"
    service.close()


def test_api_tick_and_observations_proxy(tmp_path, notifier):
    client, service = make_client(tmp_path, notifier)
    with client:
        created = client.post("/api/alarm/trigger").json()
        assert client.post("/api/tick").status_code == 200
        row = next(r for r in client.get("/api/state").json()["rounds"] if r["id"] == created["id"])
        assert row["state"] == "sent"

        obs = client.get("/api/notifier/observations")
        assert obs.status_code == 200
        items = obs.json()["items"]
        assert [i["notification_no"] for i in items] == [created["notification_no"]]
    service.close()


def test_api_observations_reports_unreachable_notifier(tmp_path, notifier):
    _store, url = notifier
    service = AlarmService(
        tmp_path / "api-alarm-2.db",
        url,
        start_worker=False,
    )
    app = create_app(service=service)
    with TestClient(app) as client:
        service._client.base_url = "http://127.0.0.1:1"  # nothing listens there
        obs = client.get("/api/notifier/observations")
        assert obs.status_code == 502
        assert "通知端不可达" in obs.json()["detail"]
    service.close()


def test_notifier_fault_modes_and_lookup(tmp_path, notifier):
    store, url = notifier
    with httpx.Client(base_url=url, timeout=5) as http:
        assert http.get("/healthz").json()["mode"] == "normal"

        reject = http.post("/api/fault", json={"mode": "reject"})
        assert reject.status_code == 200
        resp = http.post("/notify", json={"notification_no": "ALM-900001"})
        assert resp.status_code == 503
        assert resp.json()["accepted"] is False
        assert http.get("/notifications/ALM-900001").status_code == 404

        http.post("/api/fault", json={"mode": "drop_receipt"})
        resp = http.post("/notify", json={"notification_no": "ALM-900002"})
        assert resp.status_code == 504
        found = http.get("/notifications/ALM-900002")
        assert found.status_code == 200
        assert found.json()["accepted"] is True

        # Duplicate delivery attempt is idempotent at the notifier.
        http.post("/api/fault", json={"mode": "normal"})
        again = http.post("/notify", json={"notification_no": "ALM-900002"})
        assert again.json()["duplicate"] is True
        assert http.get("/notifications/ALM-900002").json()["deliveries"] == 2

        bad = http.post("/api/fault", json={"mode": "nonsense"})
        assert bad.status_code == 400
