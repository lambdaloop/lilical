"""Tests for SubscribeDialog. Uses offscreen QPA and exercises the dialog's
result-handling logic directly (bypassing the worker QThread, which would
otherwise require an event loop)."""

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


def _make_event(uid: str = "e1"):
    from lilical.models.event import Event

    return Event(
        uid=uid,
        calendar_id="",
        summary="Imported event",
        dtstart=datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
    )


def test_ok_button_disabled_when_source_empty(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    assert dialog._ok_btn is not None
    assert not dialog._ok_btn.isEnabled()
    dialog._source_edit.setText("https://example.com/cal.ics")
    assert dialog._ok_btn.isEnabled()
    dialog._source_edit.setText("   ")
    assert not dialog._ok_btn.isEnabled()
    dialog.deleteLater()


def test_on_fetch_done_success_accepts_with_suggested_name(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    dialog.canonical_source = "https://example.com/feed.ics"
    accept_calls: list[int] = []
    dialog.accept = lambda: accept_calls.append(1)  # type: ignore[method-assign]

    ev = _make_event()
    dialog._on_fetch_done((True, "", [ev], "Suggested Feed Name", "sha-1"))

    assert accept_calls == [1]
    assert dialog.display_name == "Suggested Feed Name"
    assert dialog.events == [ev]
    assert dialog.content_sha256 == "sha-1"


def test_on_fetch_done_success_uses_user_name_when_provided(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    dialog.canonical_source = "https://example.com/feed.ics"
    dialog._name_edit.setText("My Name")
    dialog.accept = lambda: None  # type: ignore[method-assign]

    dialog._on_fetch_done((True, "", [_make_event()], "Suggested", "sha"))
    assert dialog.display_name == "My Name"


def test_on_fetch_done_success_falls_back_to_url_tail(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    dialog.canonical_source = "https://example.com/team-cal.ics"
    dialog.accept = lambda: None  # type: ignore[method-assign]

    # Empty name + no X-WR-CALNAME suggestion → derive from URL tail.
    dialog._on_fetch_done((True, "", [_make_event()], None, "sha"))
    assert dialog.display_name == "team-cal"


def test_on_fetch_done_failure_stays_open_and_re_enables_inputs(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    accept_calls: list[int] = []
    dialog.accept = lambda: accept_calls.append(1)  # type: ignore[method-assign]
    # Simulate the dialog being busy mid-fetch.
    dialog._set_busy(True)

    dialog._on_fetch_done((False, "could not fetch", [], None, ""))

    assert accept_calls == []
    assert "could not fetch" in dialog._status_label.text()
    assert dialog._source_edit.isEnabled()


def test_on_fetch_done_empty_events_shows_error_and_stays_open(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    accept_calls: list[int] = []
    dialog.accept = lambda: accept_calls.append(1)  # type: ignore[method-assign]
    dialog.canonical_source = "https://example.com/feed.ics"

    dialog._on_fetch_done((True, "", [], "Feed", "sha"))

    assert accept_calls == []
    assert "no events" in dialog._status_label.text().lower()


def test_on_fetch_done_malformed_result_shows_error(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    accept_calls: list[int] = []
    dialog.accept = lambda: accept_calls.append(1)  # type: ignore[method-assign]

    dialog._on_fetch_done(("nonsense",))  # not a length-5 tuple

    assert accept_calls == []
    assert "unexpected" in dialog._status_label.text().lower()


def test_browse_button_hidden_in_web_mode_visible_in_file_mode(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    # Default: web mode → Browse hidden.
    assert not dialog._browse_btn.isVisibleTo(dialog)
    # Switch to file mode → Browse visible.
    dialog._radio_file.setChecked(True)
    dialog._on_mode_changed()
    assert dialog._browse_btn.isVisibleTo(dialog)
    dialog.deleteLater()


def test_derive_name_from_https_url(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    dialog.canonical_source = "https://example.com/path/to/holidays.ics"
    assert dialog._derive_name_from_source() == "holidays"
    dialog.deleteLater()


def test_derive_name_from_file_url(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    dialog.canonical_source = "file:///home/user/cal/birthdays.ics"
    assert dialog._derive_name_from_source() == "birthdays"
    dialog.deleteLater()


def test_default_color_is_set(qapp):
    from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

    dialog = SubscribeDialog()
    assert dialog.color.startswith("#")
    assert len(dialog.color) == 7
    dialog.deleteLater()
