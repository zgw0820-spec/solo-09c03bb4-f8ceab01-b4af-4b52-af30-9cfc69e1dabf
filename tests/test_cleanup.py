from __future__ import annotations

import sqlite3

import pytest

from app.alarm.service import AlarmConflict, AlarmNotFound, NotifyOutcome


def round_by_id(state: dict, round_id: int) -> dict | None:
    return next((r for r in state["rounds"] if r["id"] == round_id), None)


def notifier_numbers(store) -> list[str]:
    return [row["notification_no"] for row in store.recent(limit=100)]


def sent_round(alarm, clock) -> dict:
    created = alarm.trigger()
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row is not None and row["state"] == "sent"
    recovered = alarm.recover()
    assert recovered["state"] == "sent"
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["is_cleanable"], row["cleanup_block_reason"]
    return created


def test_cleanup_sent_round_removes_both_sides_and_retires_number(alarm, notifier, clock):
    store, _ = notifier
    created = sent_round(alarm, clock)
    number = created["notification_no"]
    assert notifier_numbers(store) == [number]

    result = alarm.start_cleanup(created["id"])
    assert result["complete"] is True
    assert result["deleted"] is True
    assert result["notification_no"] == number

    state = alarm.get_state()
    assert round_by_id(state, created["id"]) is None
    assert notifier_numbers(store) == []
    assert store.is_cleaned(number) == created["id"]
    assert state["counts"]["cleaning"] == 0

    # A retry after completion stays harmless and reports success.
    again = alarm.start_cleanup(created["id"])
    assert again["complete"] is True
    assert notifier_numbers(store) == []


def test_cleaned_number_is_never_reused_by_new_rounds(alarm, notifier, clock):
    store, _ = notifier
    first = sent_round(alarm, clock)
    assert first["notification_no"] == "ALM-000001"
    assert alarm.start_cleanup(first["id"])["complete"] is True

    second = alarm.trigger()
    assert second["id"] == 2
    assert second["notification_no"] != first["notification_no"]
    assert second["notification_no"] == "ALM-000002"
    alarm.tick()
    assert store.lookup(second["notification_no"]) is not None
    assert store.lookup(first["notification_no"]) is None


def test_active_pending_and_unverified_rounds_cannot_be_cleaned(alarm, notifier, clock):
    store, _ = notifier
    active = alarm.trigger()
    with pytest.raises(AlarmConflict):
        alarm.start_cleanup(active["id"])
    assert round_by_id(alarm.get_state(), active["id"])["state"] == "pending"

    # Recovered but still being verified: pending + needs_verify blocks cleanup.
    db = sqlite3.connect(alarm.db_path)
    db.execute(
        "UPDATE alarm_rounds SET state='delivering', delivery_started_at=? WHERE id=?",
        (clock(), active["id"]),
    )
    db.commit()
    db.close()
    recovered = alarm.recover()
    assert recovered["state"] == "pending" and recovered["needs_verify"] is True
    with pytest.raises(AlarmConflict):
        alarm.start_cleanup(active["id"])
    assert round_by_id(alarm.get_state(), active["id"]) is not None
    assert notifier_numbers(store) == []


def test_missing_round_raises_not_found(alarm):
    with pytest.raises(AlarmNotFound):
        alarm.start_cleanup(999)


def test_skipped_and_cancelled_rounds_are_cleanable(alarm, notifier, clock):
    store, _ = notifier
    alarm.set_maintenance(clock() + 60)
    skipped = alarm.trigger()
    alarm.recover()
    assert round_by_id(alarm.get_state(), skipped["id"])["state"] == "skipped"

    result = alarm.start_cleanup(skipped["id"])
    assert result["complete"] is True
    assert round_by_id(alarm.get_state(), skipped["id"]) is None
    assert notifier_numbers(store) == []

    alarm.end_maintenance()
    cancelled = alarm.trigger()
    alarm.recover()
    assert round_by_id(alarm.get_state(), cancelled["id"])["state"] == "cancelled"
    assert alarm.start_cleanup(cancelled["id"])["complete"] is True


def test_notifier_down_marks_cleanup_incomplete_and_retry_completes(alarm, notifier, clock):
    store, url = notifier
    created = sent_round(alarm, clock)
    number = created["notification_no"]

    alarm._client.base_url = "http://127.0.0.1:1"
    result = alarm.start_cleanup(created["id"])
    assert result["complete"] is False
    assert result["deleted"] is False
    assert result["cleanup_error"]

    row = round_by_id(alarm.get_state(), created["id"])
    assert row is not None
    assert row["state"] == "cleaning"
    assert row["cleanup_error"]
    assert row["prior_state"] == "sent"
    # The notifier still holds its record; nothing was half-deleted silently.
    assert notifier_numbers(store) == [number]

    # Retry while still unreachable stays incomplete and cannot deliver anything.
    retried = alarm.start_cleanup(created["id"])
    assert retried["complete"] is False
    alarm.tick()
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "cleaning"

    alarm._client.base_url = str(url)
    done = alarm.start_cleanup(created["id"])
    assert done["complete"] is True
    assert round_by_id(alarm.get_state(), created["id"]) is None
    assert notifier_numbers(store) == []


def test_cleaning_round_is_never_delivered_again(alarm, notifier, clock):
    store, _ = notifier
    created = sent_round(alarm, clock)
    number = created["notification_no"]
    alarm._client.base_url = "http://127.0.0.1:1"
    assert alarm.start_cleanup(created["id"])["complete"] is False

    # Restore the notifier and repeatedly tick: a cleaning row must not re-send.
    alarm._client.base_url = str(notifier[1])
    for _ in range(5):
        alarm.tick()
    rows = store.recent(limit=100)
    assert len(rows) == 1
    assert rows[0]["notification_no"] == number
    assert rows[0]["deliveries"] == 1
    assert round_by_id(alarm.get_state(), created["id"])["state"] == "cleaning"


def test_restart_with_incomplete_cleanup_is_flagged_and_retryable(alarm_factory, notifier, clock):
    store, _ = notifier
    first = alarm_factory()
    created = first.trigger()
    first.tick()
    first.recover()
    number = created["notification_no"]
    first._client.base_url = "http://127.0.0.1:1"
    assert first.start_cleanup(created["id"])["complete"] is False
    first.close()

    restarted = alarm_factory()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "cleaning"
    assert row["cleanup_error"]
    restarted.tick()
    assert len(store.recent(limit=100)) == 1  # no redelivery after restart

    assert restarted.start_cleanup(created["id"])["complete"] is True
    assert round_by_id(restarted.get_state(), created["id"]) is None
    assert notifier_numbers(store) == []
    assert store.is_cleaned(number) == created["id"]


def test_restart_between_two_deletes_does_not_duplicate_or_lose_track(
    alarm_factory, notifier, clock
):
    """Simulate a crash after the notifier delete but before the local delete."""
    store, _ = notifier
    first = alarm_factory()
    created = first.trigger()
    first.tick()
    first.recover()
    number = created["notification_no"]
    # Mark it 'cleaning' locally without contacting the notifier, then clean the
    # notifier side through its own API, as if the process died in between.
    db = sqlite3.connect(first.db_path)
    db.execute(
        "UPDATE alarm_rounds SET state='cleaning', prior_state='sent' WHERE id=?",
        (created["id"],),
    )
    db.commit()
    db.close()
    store.clean(number, created["id"])
    first.close()

    restarted = alarm_factory()
    row = round_by_id(restarted.get_state(), created["id"])
    assert row["state"] == "cleaning" and row["cleanup_error"]

    result = restarted.start_cleanup(created["id"])
    assert result["complete"] is True
    assert round_by_id(restarted.get_state(), created["id"]) is None
    assert notifier_numbers(store) == []


def test_partial_failure_is_never_shown_as_normally_sent(alarm, notifier, clock):
    store, _ = notifier
    created = sent_round(alarm, clock)
    alarm._client.base_url = "http://127.0.0.1:1"
    alarm.start_cleanup(created["id"])

    state = alarm.get_state()
    row = round_by_id(state, created["id"])
    assert row["state"] == "cleaning"
    assert row["state_label"] == "清理中"
    assert row["group"] == "cleaning"
    # Counts must not report the half-cleaned round as delivered.
    assert state["counts"]["cleaning"] == 1
    assert state["counts"]["sent"] == 0
    assert row["cleanup_error"]


def test_cleanup_does_not_touch_same_number_of_another_round(alarm, notifier, clock):
    """A notifier record with the same number but another round must survive."""
    store, _ = notifier
    created = sent_round(alarm, clock)
    number = created["notification_no"]

    # Re-key the notifier record to a different round: cleanup must refuse.
    db = sqlite3.connect(store.path)
    db.execute(
        "UPDATE notifications SET round_id = ? WHERE notification_no = ?",
        (created["id"] + 100, number),
    )
    db.commit()
    db.close()

    result = alarm.start_cleanup(created["id"])
    assert result["complete"] is False
    assert "未删除" in result["cleanup_error"]
    assert number in notifier_numbers(store)  # untouched
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "cleaning" and row["cleanup_error"]


def test_notifier_clean_endpoint_is_idempotent_and_round_scoped(notifier):
    store, _ = notifier
    from app.notifier.main import NotifyIn

    store.accept(NotifyIn(notification_no="ALM-000123", round_id=7))
    assert store.lookup("ALM-000123") is not None

    # Wrong round: refuse, keep the record.
    wrong = store.clean("ALM-000123", 8)
    assert wrong["deleted"] is False
    assert store.lookup("ALM-000123") is not None

    first = store.clean("ALM-000123", 7)
    assert first["deleted"] is True and first["existed"] is True
    assert store.lookup("ALM-000123") is None
    assert store.is_cleaned("ALM-000123") == 7

    again = store.clean("ALM-000123", 7)
    assert again["deleted"] is True and again["already_cleaned"] is True

    # The retired number cannot be accepted again, not even by the same round.
    reaccept = store.accept(NotifyIn(notification_no="ALM-000123", round_id=7))
    assert reaccept["accepted"] is False
    assert store.lookup("ALM-000123") is None


def test_new_alarm_can_start_while_old_cleanup_is_pending(alarm, notifier, clock):
    """An incomplete cleanup must not block new alarms, and must not reuse its number."""
    store, _ = notifier
    created = sent_round(alarm, clock)
    alarm._client.base_url = "http://127.0.0.1:1"
    alarm.start_cleanup(created["id"])

    nxt = alarm.trigger()
    assert nxt["notification_no"] != created["notification_no"]

    alarm._client.base_url = str(notifier[1])
    assert alarm.start_cleanup(created["id"])["complete"] is True
    alarm.tick()
    assert notifier_numbers(store) == [nxt["notification_no"]]


def test_lost_receipt_then_cleanup_resolves_by_number(alarm, notifier, clock):
    store, _ = notifier
    store.set_mode("drop_receipt")
    created = alarm.trigger()
    alarm.tick()
    row = round_by_id(alarm.get_state(), created["id"])
    assert row["state"] == "sent" and row["accepted_at"]
    alarm.recover()

    store.set_mode("normal")
    result = alarm.start_cleanup(created["id"])
    assert result["complete"] is True
    assert notifier_numbers(store) == []


def test_clean_verify_endpoint_after_cleanup(alarm, notifier, clock):
    store, _ = notifier
    created = sent_round(alarm, clock)
    number = created["notification_no"]
    alarm.start_cleanup(created["id"])

    outcome = alarm._client.verify(number)
    assert outcome.kind == "not_found"
    # The retired number is refused by the notifier even for a stale round.
    rejected = alarm._client.notify(
        {"notification_no": number, "round_id": created["id"], "device": "x"}
    )
    assert rejected.kind == "rejected"


def test_uncleanable_state_after_partial_cleanup_reports_incomplete(alarm, notifier, clock):
    """A cleanup whose notifier record is gone but with an error still retries."""
    store, _ = notifier
    created = sent_round(alarm, clock)

    calls = {"n": 0}
    original = alarm._client.clean

    def flaky(number, round_id):
        calls["n"] += 1
        return NotifyOutcome("unknown", detail="模拟清理失联")

    alarm._client.clean = flaky
    first = alarm.start_cleanup(created["id"])
    assert first["complete"] is False
    assert calls["n"] == 1

    alarm._client.clean = original
    second = alarm.retry_cleanup(created["id"])
    assert second["complete"] is True
    assert calls["n"] == 1
    assert round_by_id(alarm.get_state(), created["id"]) is None


def test_notifier_refuses_to_delete_record_with_unknown_owner(notifier):
    """An unowned same-number record is never assumed to belong to this round."""
    store, _ = notifier
    from app.notifier.main import NotifyIn

    store.accept(NotifyIn(notification_no="ALM-000555", round_id=None))
    result = store.clean("ALM-000555", 5)
    assert result["deleted"] is False
    assert result["reason"] == "round_mismatch"
    assert store.lookup("ALM-000555") is not None
    assert store.is_cleaned("ALM-000555") is None
