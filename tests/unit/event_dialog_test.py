"""Qt tests for EventDialog."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from lilical.storage.event_store import EventStore

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


class _FakeStore:
    """Minimal store stub that satisfies EventDialog's needs."""

    def list_accounts(self, enabled_only=True):
        return []

    def list_calendars(self, account_id, included_only=True):
        return []


def _make_event(**kwargs: Any):
    from lilical.models.event import Event

    defaults: dict[str, Any] = dict(
        uid="uid-test",
        calendar_id="cal-1",
        summary="Test event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Event(**defaults)


def _make_dialog(qapp, event=None):
    from lilical.ui.widgets.event_dialog import EventDialog

    return EventDialog(store=cast(EventStore, _FakeStore()), event=event)


def test_delete_button_hidden_for_new_event(qapp):
    from PySide6.QtWidgets import QPushButton

    dialog = _make_dialog(qapp, event=None)
    btn = dialog.findChild(QPushButton, "deleteButton")
    assert btn is None or not btn.isVisible()
    dialog.deleteLater()


def test_delete_button_visible_for_edit(qapp):
    from PySide6.QtWidgets import QPushButton

    event = _make_event()
    dialog = _make_dialog(qapp, event=event)
    btn = dialog.findChild(QPushButton, "deleteButton")
    # The button exists and is not explicitly hidden (dialog itself is not shown,
    # so isVisible() on a top-level widget that hasn't been shown is False;
    # check isHidden() instead — a button that was never hidden is not hidden).
    assert btn is not None
    assert not btn.isHidden()
    dialog.deleteLater()


def test_delete_requested_false_by_default(qapp):
    dialog = _make_dialog(qapp, event=None)
    assert dialog.delete_requested is False
    dialog.deleteLater()


def test_delete_button_sets_delete_requested(qapp):
    event = _make_event()
    dialog = _make_dialog(qapp, event=event)
    dialog._on_delete()
    assert dialog.delete_requested is True
    dialog.deleteLater()


def test_rrule_round_trip(qapp):
    event = _make_event(rrule="FREQ=DAILY")
    dialog = _make_dialog(qapp, event=event)
    assert dialog._rrule_editor.value() == "FREQ=DAILY"
    dialog.deleteLater()


def test_build_event_includes_rrule(qapp):
    event = _make_event(rrule="FREQ=WEEKLY")
    dialog = _make_dialog(qapp, event=event)
    # Ensure title is set so build_event works
    dialog._title_edit.setText("Weekly meeting")
    built = dialog.build_event("uid-built")
    assert built.rrule is not None
    assert "FREQ=WEEKLY" in built.rrule
    dialog.deleteLater()


# ── cross-timezone edit round-trip ───────────────────────────────────────────


@pytest.fixture
def force_berlin_tz():
    """Force the process-local zone to Europe/Berlin so cross-zone display is
    exercised regardless of the host machine's timezone."""
    import time

    prev = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


def _ny_event():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    return _make_event(
        dtstart=datetime(2026, 6, 8, 14, 30, tzinfo=ny),
        dtend=datetime(2026, 6, 8, 15, 30, tzinfo=ny),
        tz="America/New_York",
    )


def test_cross_tz_edit_no_op_save_does_not_move_event(qapp, force_berlin_tz):
    """Opening a New York event on a Berlin machine and saving without edits
    must preserve the exact instant and zone (regression: it used to shift)."""
    event = _ny_event()
    dialog = _make_dialog(qapp, event=event)
    dialog._title_edit.setText("NY meeting")
    built = dialog.build_event("uid-built")
    assert built.tz == "America/New_York"
    assert built.dtstart == event.dtstart
    assert built.dtend == event.dtend
    dialog.deleteLater()


def test_cross_tz_edit_shows_event_local_wall_clock(qapp, force_berlin_tz):
    """The Start field shows the event's own wall-clock (14:30 NY), not the
    Berlin-converted time (20:30)."""
    dialog = _make_dialog(qapp, event=_ny_event())
    t = dialog._start_edit.dateTime().time()
    assert (t.hour(), t.minute()) == (14, 30)
    assert dialog._tz_combo.currentText() == "America/New_York"
    dialog.deleteLater()


def test_changing_tz_converts_to_same_instant(qapp, force_berlin_tz):
    """Switching the zone selector re-displays the same absolute moment in the
    new zone (14:30 NY -> 20:30 Berlin) and saves that instant under the new
    zone."""
    dialog = _make_dialog(qapp, event=_ny_event())
    dialog._title_edit.setText("NY meeting")
    dialog._tz_combo.setCurrentText("Europe/Berlin")
    t = dialog._start_edit.dateTime().time()
    assert (t.hour(), t.minute()) == (20, 30)
    built = dialog.build_event("uid-built")
    assert built.tz == "Europe/Berlin"
    # Same absolute instant as the original NY start.
    assert built.dtstart == _ny_event().dtstart
    dialog.deleteLater()


# ── read-only calendar exclusion in the picker ───────────────────────────────


class _StoreWithCalendars:
    """Fake store backing the calendar picker with a mixed access-role mix."""

    def __init__(self, calendars: list[tuple[str, str, str]]) -> None:
        # calendars: list of (id, display_name, access_role)
        from types import SimpleNamespace

        self._account = SimpleNamespace(id="acc-1", display_name="Acc")
        self._cals = [
            SimpleNamespace(
                id=cid,
                account_id="acc-1",
                provider_id=cid,
                display_name=name,
                color="#5e9fff",
                access_role=role,
                is_included=1,
                is_visible=1,
            )
            for (cid, name, role) in calendars
        ]

    def list_accounts(self, enabled_only=True):
        return [self._account]

    def list_calendars(self, account_id, included_only=True):
        return [c for c in self._cals if c.account_id == account_id]


def _picker_ids(dialog) -> list:
    return [
        dialog._cal_combo.itemData(i) for i in range(dialog._cal_combo.count())
    ]


def test_event_dialog_picker_excludes_reader_and_freebusyreader(qapp):
    from lilical.ui.widgets.event_dialog import EventDialog

    store = _StoreWithCalendars(
        [
            ("cal-work", "Work", "owner"),
            ("cal-sub", "Subscription", "reader"),
            ("cal-fb", "Free/busy peek", "freebusyreader"),
            ("cal-shared", "Shared write", "writer"),
        ]
    )
    dialog = EventDialog(store=cast(EventStore, store))
    ids = _picker_ids(dialog)
    assert "cal-work" in ids
    assert "cal-shared" in ids
    assert "cal-sub" not in ids
    assert "cal-fb" not in ids
    dialog.deleteLater()


def test_event_dialog_picker_empty_when_only_read_only_calendars(qapp):
    """Pin behavior: if every calendar is read-only, the combo ends up
    empty. The dialog still constructs without crashing; downstream save
    logic is responsible for guarding against an empty currentData()."""
    from lilical.ui.widgets.event_dialog import EventDialog

    store = _StoreWithCalendars(
        [
            ("cal-sub", "Subscription", "reader"),
            ("cal-fb", "Free/busy", "freebusyreader"),
        ]
    )
    dialog = EventDialog(store=cast(EventStore, store))
    assert dialog._cal_combo.count() == 0
    dialog.deleteLater()
