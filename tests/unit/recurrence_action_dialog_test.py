"""Qt dialog tests for RecurrenceActionDialog."""

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


def _make_dialog(qapp, action="edit"):
    from lilical.ui.widgets.recurrence_action_dialog import RecurrenceActionDialog

    return RecurrenceActionDialog(action=action)


def test_choice_defaults_to_none(qapp):
    dialog = _make_dialog(qapp)
    assert dialog.choice is None


def test_occurrence_button_sets_choice(qapp):
    dialog = _make_dialog(qapp)
    dialog._pick("occurrence")
    assert dialog.choice == "occurrence"


def test_following_button_sets_choice(qapp):
    dialog = _make_dialog(qapp)
    dialog._pick("following")
    assert dialog.choice == "following"


def test_series_button_sets_choice(qapp):
    dialog = _make_dialog(qapp)
    dialog._pick("series")
    assert dialog.choice == "series"
