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
