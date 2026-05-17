"""Tests for extractable logic in WeekView (no full rendering)."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_week_view(qapp):
    from unittest.mock import MagicMock

    from lilical.ui.views.week import WeekView

    return WeekView(store=MagicMock())


def test_snap_minutes_to(qapp):
    view = _make_week_view(qapp)
    view._snap_minutes = 15
    assert view._snap_minutes_to(7) == 0
    assert view._snap_minutes_to(8) == 15
    assert view._snap_minutes_to(100) == 105


def test_format_drag_label_move(qapp):
    view = _make_week_view(qapp)
    view._start = date(2026, 5, 18)
    label = view._format_drag_label("move", 0, 540, 600)
    assert "Mon" in label
    assert "09:00" in label
    assert "10:00" in label


def test_format_drag_label_create(qapp):
    view = _make_week_view(qapp)
    view._start = date(2026, 5, 18)
    label = view._format_drag_label("create", 0, 540, 630)
    assert "09:00" in label
    assert "10:30" in label
    assert "1h 30m" in label


def test_format_drag_label_create_short(qapp):
    view = _make_week_view(qapp)
    view._start = date(2026, 5, 18)
    label = view._format_drag_label("create", 0, 540, 600)
    assert "1h" in label or "60m" in label
