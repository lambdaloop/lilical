"""Tests for EventStore completion methods: set_completed, is_completed,
completion_for_instances."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lilical.models.account import Account
from lilical.models.calendar import Calendar
from lilical.models.db import Base
from lilical.models.event import EventInstanceRow
from lilical.storage.event_store import EventStore


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    with Session(e) as s, s.begin():
        s.add(
            Account(
                id="acc-1",
                kind="google",
                display_name="Work",
                identity="test@example.com",
                secret_ref="google:acc-1",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        s.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="primary",
                display_name="Primary",
                color="#5e9fff",
                is_primary=1,
                is_visible=1,
                access_role="owner",
            )
        )
    return e


@pytest.fixture
def store(engine):
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    return EventStore(engine)


def _make_instance(calendar_id: str, uid: str, dtstart_utc: int) -> EventInstanceRow:
    return EventInstanceRow(
        uid=uid,
        calendar_id=calendar_id,
        dtstart_utc=dtstart_utc,
        dtend_utc=dtstart_utc + 3600,
        dtstart_local=_utc(dtstart_utc).isoformat(),
        dtend_local=_utc(dtstart_utc + 3600).isoformat(),
        all_day=0,
        is_override=0,
        recurrence_id="",
    )


def _add_instance(engine, inst: EventInstanceRow) -> EventInstanceRow:
    uid, cal_id, dts = inst.uid, inst.calendar_id, inst.dtstart_utc
    with Session(engine) as s, s.begin():
        s.add(inst)
    # Re-query in a new session and expunge to get a fully-loaded detached object.
    with Session(engine) as s:
        row = (
            s.query(EventInstanceRow)
            .filter_by(uid=uid, calendar_id=cal_id, dtstart_utc=dts)
            .first()
        )
        s.expunge(row)
    return row


# ── is_completed ─────────────────────────────────────────────────────────────


def test_is_completed_false_initially(store):
    assert not store.is_completed("cal-1", "uid-a", 1000)


def test_set_completed_true_then_is_completed(store):
    store.set_completed("cal-1", "uid-a", 1000, True)
    assert store.is_completed("cal-1", "uid-a", 1000)


def test_set_completed_false_removes_row(store):
    store.set_completed("cal-1", "uid-a", 1000, True)
    store.set_completed("cal-1", "uid-a", 1000, False)
    assert not store.is_completed("cal-1", "uid-a", 1000)


def test_set_completed_false_noop_when_not_set(store):
    # Should not raise even when no row exists.
    store.set_completed("cal-1", "uid-a", 1000, False)
    assert not store.is_completed("cal-1", "uid-a", 1000)


def test_set_completed_idempotent(store):
    store.set_completed("cal-1", "uid-a", 1000, True)
    store.set_completed("cal-1", "uid-a", 1000, True)  # second call should not raise
    assert store.is_completed("cal-1", "uid-a", 1000)


def test_completion_key_is_per_occurrence(store):
    store.set_completed("cal-1", "uid-a", 1000, True)
    assert not store.is_completed("cal-1", "uid-a", 2000)


# ── completion_for_instances ─────────────────────────────────────────────────


def test_completion_for_instances_empty_list(store):
    result = store.completion_for_instances([])
    assert result == frozenset()


def test_completion_for_instances_none_completed(store, engine):
    inst = _add_instance(engine, _make_instance("cal-1", "uid-b", 5000))
    result = store.completion_for_instances([inst])
    assert result == frozenset()


def test_completion_for_instances_one_completed(store, engine):
    inst = _add_instance(engine, _make_instance("cal-1", "uid-c", 6000))
    store.set_completed("cal-1", "uid-c", 6000, True)
    result = store.completion_for_instances([inst])
    assert ("cal-1", "uid-c", 6000) in result


def test_completion_for_instances_selective(store, engine):
    inst1 = _add_instance(engine, _make_instance("cal-1", "uid-d", 7000))
    inst2 = _add_instance(engine, _make_instance("cal-1", "uid-e", 8000))
    store.set_completed("cal-1", "uid-d", 7000, True)
    result = store.completion_for_instances([inst1, inst2])
    assert ("cal-1", "uid-d", 7000) in result
    assert ("cal-1", "uid-e", 8000) not in result


def test_completion_signal_emitted(store, engine):
    received = []
    store.instance_completion_changed.connect(
        lambda cal, uid, dt: received.append((cal, uid, dt))
    )
    store.set_completed("cal-1", "uid-f", 9000, True)
    assert received == [("cal-1", "uid-f", 9000)]


def test_completion_signal_emitted_on_remove(store, engine):
    received = []
    store.set_completed("cal-1", "uid-g", 10000, True)
    store.instance_completion_changed.connect(
        lambda cal, uid, dt: received.append((cal, uid, dt))
    )
    store.set_completed("cal-1", "uid-g", 10000, False)
    assert received == [("cal-1", "uid-g", 10000)]
