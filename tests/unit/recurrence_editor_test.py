"""Qt widget tests for RecurrenceEditor."""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_editor(qapp):
    from lilical.ui.widgets.recurrence_editor import RecurrenceEditor

    return RecurrenceEditor()


def test_default_value_is_none(qapp):
    editor = _make_editor(qapp)
    assert editor.value() is None


def test_daily_freq_round_trip(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=DAILY")
    assert editor.value() == "FREQ=DAILY"


def test_daily_with_interval(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=DAILY;INTERVAL=3")
    val = editor.value()
    assert val is not None
    assert "INTERVAL=3" in val


def test_weekly_with_byday(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=WEEKLY;BYDAY=MO,WE")
    val = editor.value()
    assert val is not None
    # Order may vary; check both codes are present
    assert "MO" in val
    assert "WE" in val
    assert "BYDAY=" in val


def test_monthly(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=MONTHLY")
    val = editor.value()
    assert val is not None
    assert val.startswith("FREQ=MONTHLY")


def test_yearly(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=YEARLY")
    val = editor.value()
    assert val is not None
    assert val.startswith("FREQ=YEARLY")


def test_count_endpoint(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=DAILY;COUNT=5")
    val = editor.value()
    assert val is not None
    assert "COUNT=5" in val


def test_until_endpoint(qapp):
    editor = _make_editor(qapp)
    editor.set_value("FREQ=WEEKLY;UNTIL=20270101T000000Z")
    val = editor.value()
    assert val is not None
    assert "UNTIL=" in val


def test_none_freq_from_value(qapp):
    editor = _make_editor(qapp)
    editor.set_value(None)
    assert editor.value() is None


def test_set_value_none_rrule(qapp):
    editor = _make_editor(qapp)
    editor.set_value("")
    assert editor.value() is None
