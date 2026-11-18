from __future__ import annotations

import sqlite3


def round_by_id(state: dict, round_id: int) -> dict:
    return next(r for r in state["rounds"] if r["id"] == round_id)


def notifier_rows(store) -> list[dict]:
    return store.recent(limit=100)


def test_outside_maintenance_notifies_immediately_and_only_once(alarm, notifier):
    store, _ = notifier
    created = alarm.trigger()
    assert created["state"] == "pending"
    assert created["notification_no"] == "ALM-000001"

    alarm.tick()
    state = alarm.get_state()
    row = round_by_id(state, created["id"])
    assert row["state"] == "sent"
    assert row["accepted_at"] is not None

    # Repeated ticks must not produce another notification.
    for _ in range(5):
        alarm.tick()
    rows = notifier_rows(store)
    assert len(rows) == 1
    assert rows[0]["notification_no"] == created["notification_no"]
    assert rows[0]["deliveries"] == 1
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "sent"


def test_maintenance_suppresses_then_recovered_round_is_not_backfilled(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 60)
    created = alarm.trigger()
    assert created["state"] == "suppressed"

    alarm.tick()
    assert notifier_rows(store) == []

    recovered = alarm.recover()
    assert recovered["state"] == "skipped"

    clock.advance(61)
    alarm.tick()
    state = alarm.get_state()
    assert round_by_id(state, created["id"])["state"] == "skipped"
    assert notifier_rows(store) == []


def test_maintenance_expiry_sends_active_round_with_fixed_number(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 60)
    created = alarm.trigger()
    number = created["notification_no"]
    assert created["state"] == "suppressed"

    clock.advance(61)
    # Expiry happens on read as well; then a tick delivers.
    state = alarm.get_state()
    assert round_by_id(state, created["id"])["state"] == "pending"
    alarm.tick()

    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "sent"
    rows = notifier_rows(store)
    assert [r["notification_no"] for r in rows] == [number]

    # Recovery afterwards keeps the accepted record.
    recovered = alarm.recover()
    assert recovered["state"] == "sent"
    assert recovered["accepted_at"] is not None
    alarm.tick()
    assert len(notifier_rows(store)) == 1


def test_maintenance_end_button_releases_suppressed_round(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 3600)
    created = alarm.trigger()
    assert created["state"] == "suppressed"

    alarm.end_maintenance()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "pending"
    alarm.tick()
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "sent"
    assert len(notifier_rows(store)) == 1


def test_triggers_outside_maintenance_are_not_suppressed(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 60)
    suppressed = alarm.trigger()
    alarm.recover()
    assert round_by_id(alarm.get_state(), suppressed["id"])["state"] == "skipped"

    clock.advance(61)
    alarm.get_state()
    outside = alarm.trigger()
    assert outside["state"] == "pending"
    assert outside["maintenance_id"] is None
    alarm.tick()
    assert round_by_id(alarm.get_state(), outside["id"])["state"] == "sent"
    assert [r["notification_no"] for r in notifier_rows(store)] == [outside["notification_no"]]


def test_not_accepted_is_never_reported_sent(alarm, notifier, clock):
    store, _ = notifier
    store.set_mode("reject")

    created = alarm.trigger()
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "pending"
    assert row["accepted_at"] is None
    assert row["needs_verify"] is False
    assert notifier_rows(store) == []

    clock.advance(1)
    alarm.tick()
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "pending"
    assert notifier_rows(store) == []

    store.set_mode("normal")
    clock.advance(30)
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "sent"
    rows = notifier_rows(store)
    assert len(rows) == 1
    assert rows[0]["notification_no"] == created["notification_no"]


def test_lost_receipt_is_resolved_by_notification_number(alarm, notifier, clock):
    store, _ = notifier
    store.set_mode("drop_receipt")

    created = alarm.trigger()
    alarm.tick()

    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "sent"
    assert row["accepted_at"] is not None
    rows = notifier_rows(store)
    assert len(rows) == 1
    assert rows[0]["notification_no"] == created["notification_no"]
    # The receipt was lost, but the notification itself must not be repeated.
    assert rows[0]["deliveries"] == 1


def test_lost_receipt_with_unverifiable_state_stays_pending(alarm, notifier, monkeypatch, clock):
    store, _ = notifier
    from app.alarm.service import NotifyOutcome

    monkeypatch.setattr(
        alarm._client, "notify", lambda payload: NotifyOutcome("unknown", detail="超时")
    )
    monkeypatch.setattr(
        alarm._client, "verify", lambda number: NotifyOutcome("unknown", detail="核实超时")
    )
    created = alarm.trigger()
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "pending"
    assert row["needs_verify"] is True
    assert row["accepted_at"] is None
    assert notifier_rows(store) == []


def test_restart_resumes_by_verifying_before_resending(alarm_factory, notifier, clock):
    store, _ = notifier
    service = alarm_factory()
    store.set_mode("drop_receipt")
    created = service.trigger()
    service.tick()
    assert round_by_id(service.get_state(), created["id"])["state"] == "sent"
    service.stop_worker()
    service.close()

    # Simulate a crash window: the row was left mid-delivery though it was accepted.
    db = sqlite3.connect(service.db_path)
    db.execute(
        "UPDATE alarm_rounds SET state='delivering', accepted_at=NULL, needs_verify=0 WHERE id=?",
        (created["id"],),
    )
    db.commit()
    db.close()

    store.set_mode("normal")
    restarted = alarm_factory()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "pending"
    assert row["needs_verify"] is True

    restarted.tick()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "sent"
    assert row["accepted_at"] is not None
    rows = notifier_rows(store)
    assert len(rows) == 1
    assert rows[0]["deliveries"] == 1  # verification only, no second notification


def test_recovered_before_delivery_starts_is_cancelled(alarm, notifier, clock):
    store, _ = notifier
    created = alarm.trigger()
    # No tick yet: delivery has not started.
    recovered = alarm.recover()
    assert recovered["state"] == "cancelled"
    alarm.tick()
    assert notifier_rows(store) == []
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "cancelled"


def test_recovered_after_delivery_started_is_kept_and_not_duplicated(alarm, notifier, clock):
    store, _ = notifier
    created = alarm.trigger()
    # Put the round into "delivery started but result unknown" as if mid-flight.
    db = sqlite3.connect(alarm.db_path)
    db.execute(
        "UPDATE alarm_rounds SET state='delivering', delivery_started_at=? WHERE id=?",
        (clock(), created["id"]),
    )
    db.commit()
    db.close()

    recovered = alarm.recover()
    assert recovered["state"] == "pending"
    assert recovered["needs_verify"] is True
    assert "投递已开始" in recovered["note"]

    # The notifier already accepted it; reconcile marks sent, never re-notifies.
    store.accept(
        __import__("app.notifier.main", fromlist=["NotifyIn"]).NotifyIn(
            notification_no=created["notification_no"], round_id=created["id"]
        )
    )
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "sent"
    rows = notifier_rows(store)
    assert len(rows) == 1
    assert rows[0]["deliveries"] == 1


def test_recovery_after_failed_send_stops_retrying(alarm, notifier, clock):
    store, _ = notifier
    store.set_mode("reject")
    created = alarm.trigger()
    alarm.tick()
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "pending"

    recovered = alarm.recover()
    assert recovered["state"] == "cancelled"
    clock.advance(60)
    alarm.tick()
    assert notifier_rows(store) == []
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "cancelled"


def test_trigger_and_recover_conflicts(alarm):
    import pytest
    from app.alarm.service import AlarmConflict

    created = alarm.trigger()
    with pytest.raises(AlarmConflict):
        alarm.trigger()
    alarm.recover()
    with pytest.raises(AlarmConflict):
        alarm.recover()
    assert created["id"] == 1


def test_extending_cutoff_moves_suppressed_delivery(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 30)
    created = alarm.trigger()
    assert created["state"] == "suppressed"

    # Operator extends maintenance; the round must remain suppressed past the old cutoff.
    alarm.set_maintenance(clock() + 120)
    clock.advance(31)
    alarm.tick()
    assert notifier_rows(store) == []
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "suppressed"

    clock.advance(90)
    alarm.tick()
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "sent"
    assert [r["notification_no"] for r in notifier_rows(store)] == [created["notification_no"]]


def test_manual_end_skips_recovered_suppressed_round(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 3600)
    created = alarm.trigger()
    alarm.recover()
    window = alarm.end_maintenance()
    assert window["close_reason"] == "manual"
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "skipped"
    assert "检修结束前" in row["note"]
    alarm.tick()
    assert notifier_rows(store) == []


def test_notifier_down_never_reported_sent_and_recovery_clears_it(alarm, notifier, clock):
    store, _ = notifier
    created = alarm.trigger()
    alarm._client.base_url = "http://127.0.0.1:1"

    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "pending"
    assert row["needs_verify"] is True
    assert row["accepted_at"] is None
    assert row["last_error"] is not None

    clock.advance(2)
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "pending"
    assert row["accepted_at"] is None

    # Alert clears while the notifier is unreachable: reconcile by number, never guess.
    recovered = alarm.recover()
    assert recovered["needs_verify"] is True
    assert recovered["accepted_at"] is None

    # Notifier comes back without this number: round is cancelled, nothing delivered.
    alarm._client.base_url = str(notifier[1])
    clock.advance(60)
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "cancelled"
    assert notifier_rows(store) == []


def test_cutoff_passed_during_downtime_is_released_on_restart(alarm_factory, notifier, clock):
    store, _ = notifier
    first = alarm_factory()
    first.set_maintenance(clock() + 10)
    created = first.trigger()
    assert created["state"] == "suppressed"
    first.close()

    clock.advance(11)
    restarted = alarm_factory()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "pending"
    assert restarted.get_state()["maintenance"] is None

    restarted.tick()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "sent"
    assert [r["notification_no"] for r in notifier_rows(store)] == [created["notification_no"]]
