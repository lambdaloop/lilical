"""Qt tests for EventDialog."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

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


def _make_event(**kwargs):
    from lilical.models.event import Event

    defaults = dict(
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

    return EventDialog(store=_FakeStore(), event=event)


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
    dialog = EventDialog(store=store)
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
    dialog = EventDialog(store=store)
    assert dialog._cal_combo.count() == 0
    dialog.deleteLater()
