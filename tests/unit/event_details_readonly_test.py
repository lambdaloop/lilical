"""Tests that EventDetailsDialog hides Edit/Delete on read-only calendars."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_event():
    from lilical.models.event import Event

    return Event(
        uid="uid-1",
        calendar_id="cal-1",
        summary="Subscription event",
        dtstart=datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
    )


class _FakeStore:
    """Minimal store stub with a configurable calendar access_role."""

    def __init__(self, access_role: str) -> None:
        self._cal = SimpleNamespace(
            id="cal-1",
            account_id="acc-1",
            display_name="Cal",
            color="#5e9fff",
            access_role=access_role,
            provider_id="pid",
        )
        self._account = SimpleNamespace(id="acc-1", display_name="Acc")

    def get_calendar(self, calendar_id: str):
        return self._cal if calendar_id == "cal-1" else None

    def get_account(self, account_id: str):
        return self._account if account_id == "acc-1" else None

    def list_accounts(self, enabled_only: bool = True):
        return [self._account]

    def list_calendars(self, account_id: str, included_only: bool = True):
        return [self._cal] if account_id == "acc-1" else []

    def is_completed(self, *_args, **_kwargs) -> bool:
        return False


def _button_labels(dialog) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in dialog.findChildren(QPushButton)]


def test_read_only_calendar_hides_edit_and_delete(qapp):
    from lilical.ui.widgets.event_details_dialog import EventDetailsDialog

    dialog = EventDetailsDialog(
        store=_FakeStore(access_role="reader"),
        event=_make_event(),
    )
    labels = _button_labels(dialog)
    assert "Edit" not in labels
    assert "Delete" not in labels
    assert "Close" in labels
    dialog.deleteLater()


def test_freebusyreader_calendar_hides_edit_and_delete(qapp):
    from lilical.ui.widgets.event_details_dialog import EventDetailsDialog

    dialog = EventDetailsDialog(
        store=_FakeStore(access_role="freebusyreader"),
        event=_make_event(),
    )
    labels = _button_labels(dialog)
    assert "Edit" not in labels
    assert "Delete" not in labels
    dialog.deleteLater()


def test_owner_calendar_shows_edit_and_delete(qapp):
    from lilical.ui.widgets.event_details_dialog import EventDetailsDialog

    dialog = EventDetailsDialog(
        store=_FakeStore(access_role="owner"),
        event=_make_event(),
    )
    labels = _button_labels(dialog)
    assert "Edit" in labels
    assert "Delete" in labels
    dialog.deleteLater()


def test_writer_calendar_shows_edit_and_delete(qapp):
    """Anything not in {reader, freebusyreader} is writable."""
    from lilical.ui.widgets.event_details_dialog import EventDetailsDialog

    dialog = EventDetailsDialog(
        store=_FakeStore(access_role="writer"),
        event=_make_event(),
    )
    labels = _button_labels(dialog)
    assert "Edit" in labels
    assert "Delete" in labels
    dialog.deleteLater()
