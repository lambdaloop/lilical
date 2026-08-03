"""Tests for _recurrence_actions.py dispatch logic."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import pytest

from lilical.storage.event_store import EventStore

if TYPE_CHECKING:
    from lilical.models.event import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


class _FakeStore:
    def __init__(self):
        self.update_calls = []
        self.update_instance_calls = []
        self.split_series_calls = []
        self.truncate_calls = []
        self.delete_calls = []
        self.delete_instance_calls = []

    def queue_update(self, event, etag):
        self.update_calls.append((event, etag))

    def queue_update_instance(self, uid, calendar_id, recurrence_id_dt, edited):
        self.update_instance_calls.append((uid, calendar_id, recurrence_id_dt, edited))

    def queue_split_series(self, uid, calendar_id, split_at_dt, edited_event_for_tail):
        self.split_series_calls.append(
            (uid, calendar_id, split_at_dt, edited_event_for_tail)
        )
        return "new-uid"

    def queue_truncate_series(self, uid, calendar_id, until_dt):
        self.truncate_calls.append((uid, calendar_id, until_dt))

    def queue_delete(self, uid, calendar_id):
        self.delete_calls.append((uid, calendar_id))

    def queue_delete_instance(self, uid, calendar_id, recurrence_id_dt):
        self.delete_instance_calls.append((uid, calendar_id, recurrence_id_dt))

    def get_event(self, uid: str, calendar_id: str) -> "Event | None":
        return None


def _make_event(**kwargs: Any):
    from lilical.models.event import Event

    defaults: dict[str, Any] = dict(
        uid="uid-series",
        calendar_id="cal-1",
        summary="Recurring Meeting",
        rrule="FREQ=WEEKLY",
    )
    defaults.update(kwargs)
    return Event(**defaults)


def test_dispatch_edit_occurrence(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    instance_dt = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    event = _make_event()
    edited = _make_event(summary="Edited")

    _dispatch_edit(parent, store, event, instance_dt, edited, "occurrence")

    assert len(store.update_instance_calls) == 1
    uid, _cal_id, rid, _ed = store.update_instance_calls[0]
    assert uid == "uid-series"
    assert rid == instance_dt

    parent.deleteLater()


def test_dispatch_edit_following_calls_split_series(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    instance_dt = datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc)
    event = _make_event()
    edited = _make_event(summary="Edited tail")

    _dispatch_edit(parent, store, event, instance_dt, edited, "following")

    assert len(store.split_series_calls) == 1
    uid, _cal_id, split_at, _tail = store.split_series_calls[0]
    assert uid == "uid-series"
    assert split_at == instance_dt

    parent.deleteLater()


def test_dispatch_edit_series_calls_queue_update(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    event = _make_event()
    edited = _make_event(summary="Series Edit")

    _dispatch_edit(parent, store, event, None, edited, "series")

    assert len(store.update_calls) == 1

    parent.deleteLater()


def test_dispatch_delete_occurrence_calls_delete_instance(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    instance_dt = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    event = _make_event()

    _dispatch_delete(parent, store, event, instance_dt, "occurrence")

    assert len(store.delete_instance_calls) == 1
    uid, _cal_id, rid = store.delete_instance_calls[0]
    assert uid == "uid-series"
    assert rid == instance_dt

    parent.deleteLater()


def test_dispatch_delete_following_calls_truncate(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    instance_dt = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    event = _make_event()

    _dispatch_delete(parent, store, event, instance_dt, "following")

    assert len(store.truncate_calls) == 1
    uid, _cal_id, until_dt = store.truncate_calls[0]
    assert uid == "uid-series"
    assert until_dt == instance_dt

    parent.deleteLater()


def test_dispatch_edit_occurrence_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Edited via rid")

    _dispatch_edit(parent, store, event, None, edited, "occurrence")

    assert len(store.update_instance_calls) == 1
    _uid, _cal_id, rid_call, _ed = store.update_instance_calls[0]
    assert rid_call == rid
    parent.deleteLater()


def test_dispatch_edit_following_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Split via rid")

    _dispatch_edit(parent, store, event, None, edited, "following")

    assert len(store.split_series_calls) == 1
    parent.deleteLater()


def test_dispatch_edit_series_with_recurrence_id_looks_up_master(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    class _MasterLookupStore(_FakeStore):
        def get_event(self, uid, calendar_id):
            return _make_event(etag='"master-etag"')

    store = cast(EventStore, _MasterLookupStore())
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Series edit via rid")

    _dispatch_edit(parent, store, event, None, edited, "series")

    assert len(store.update_calls) == 1
    parent.deleteLater()


def test_dispatch_delete_occurrence_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = cast(EventStore, _FakeStore())
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)

    _dispatch_delete(parent, store, event, None, "occurrence")

    assert len(store.delete_instance_calls) == 1
    parent.deleteLater()


def test_dispatch_delete_following_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    class _MasterLookupStore(_FakeStore):
        def get_event(self, uid, calendar_id):
            return _make_event()

    store = cast(EventStore, _MasterLookupStore())
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)

    _dispatch_delete(parent, store, event, None, "following")

    assert len(store.truncate_calls) == 1
    parent.deleteLater()


# ── dispatch_drag_edit: dragging a recurring chip must prompt for scope ───────


def _drag_dialog(monkeypatch, choice: str | None, *, accepted: bool = True):
    """Stub RecurrenceActionDialog so the drag dispatch runs headless."""
    import lilical.ui.widgets.recurrence_action_dialog as rad_mod

    class _StubDialog:
        def __init__(self, parent=None, *, action="edit"):
            self.action = action

        def exec(self):
            return 1 if accepted else 0

        @property
        def choice(self):
            return choice

    monkeypatch.setattr(rad_mod, "RecurrenceActionDialog", _StubDialog)


def test_drag_recurring_chip_prompts_and_updates_only_the_occurrence(
    qapp, monkeypatch
) -> None:
    """Dragging one occurrence must not rewrite the whole series.

    This used to call queue_update directly on the master, so dragging a single
    occurrence of a weekly meeting moved every occurrence — with no prompt.
    """
    from lilical.ui.views._recurrence_actions import dispatch_drag_edit

    _drag_dialog(monkeypatch, "occurrence")
    store = _FakeStore()
    inst = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    new_start = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)
    new_end = datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc)

    ok = dispatch_drag_edit(
        cast(Any, None),
        cast(EventStore, store),
        event,
        inst,
        new_start,
        new_end,
    )

    assert ok
    assert store.update_calls == [], "the whole series was rewritten"
    assert len(store.update_instance_calls) == 1
    uid, cal, rid, edited = store.update_instance_calls[0]
    assert rid == inst
    assert edited.dtstart == new_start
    assert edited.rrule is None


def test_drag_recurring_chip_series_shifts_master_by_delta(qapp, monkeypatch) -> None:
    """'Entire series' must shift the master, not stamp the dragged date onto it.

    Stamping the absolute date would drag the series' start to whichever
    occurrence happened to be on screen.
    """
    from lilical.ui.views._recurrence_actions import dispatch_drag_edit

    _drag_dialog(monkeypatch, "series")
    store = _FakeStore()
    master_start = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    event = _make_event(
        dtstart=master_start,
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    # User drags the 20 May occurrence five hours later.
    inst = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    new_start = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)
    new_end = datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc)

    dispatch_drag_edit(
        cast(Any, None), cast(EventStore, store), event, inst, new_start, new_end
    )

    assert len(store.update_calls) == 1
    updated, _etag = store.update_calls[0]
    # Same day as the master, five hours later — not moved to 20 May.
    assert updated.dtstart == master_start.replace(hour=14)
    assert updated.dtend == master_start.replace(hour=15)


def test_drag_non_recurring_chip_does_not_prompt(qapp, monkeypatch) -> None:
    """One-off events keep the old direct-update path."""
    from lilical.ui.views._recurrence_actions import dispatch_drag_edit

    _drag_dialog(monkeypatch, None, accepted=False)  # would cancel if consulted
    store = _FakeStore()
    event = _make_event(
        rrule=None,
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    new_start = datetime(2026, 5, 13, 14, 0, tzinfo=timezone.utc)

    ok = dispatch_drag_edit(
        cast(Any, None),
        cast(EventStore, store),
        event,
        None,
        new_start,
        new_start.replace(hour=15),
    )

    assert ok
    assert len(store.update_calls) == 1
    assert store.update_instance_calls == []


def test_drag_recurring_chip_cancelled_makes_no_change(qapp, monkeypatch) -> None:
    """Cancelling the scope prompt must leave the event untouched."""
    from lilical.ui.views._recurrence_actions import dispatch_drag_edit

    _drag_dialog(monkeypatch, None, accepted=False)
    store = _FakeStore()
    event = _make_event(
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    new_start = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)

    ok = dispatch_drag_edit(
        cast(Any, None),
        cast(EventStore, store),
        event,
        datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        new_start,
        new_start.replace(hour=15),
    )

    assert ok is False
    assert store.update_calls == []
    assert store.update_instance_calls == []
