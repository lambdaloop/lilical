"""Tests for WeekView.range_label date formatting.

range_label uses only self._start (date) and self._day_count (int), so we can
test it via the unbound method on a simple namespace without a full WeekView.
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from lilical.ui.views.week import WeekView  # noqa: E402


def _label(start: date, day_count: int) -> str:
    ns = SimpleNamespace(_start=start, _day_count=day_count)
    return WeekView.range_label(ns)  # type: ignore[arg-type]


def test_single_day() -> None:
    assert _label(date(2026, 5, 13), 1) == "May 13–13, 2026"


def test_full_week_same_month() -> None:
    assert _label(date(2026, 5, 11), 7) == "May 11–17, 2026"


def test_cross_month() -> None:
    # May 27 + 6 days = June 2 — crosses the month boundary
    label = _label(date(2026, 5, 27), 7)
    assert "May" in label and "Jun" in label


def test_cross_year() -> None:
    label = _label(date(2025, 12, 29), 7)
    assert "Dec" in label and "Jan" in label
    # Verify both years appear in the label
    assert "2026" in label


def test_five_day_work_week_same_month() -> None:
    label = _label(date(2026, 6, 1), 5)
    assert label == "June 1–5, 2026"


def test_cross_month_boundary_short_span() -> None:
    # Jan 31 – Feb 3
    label = _label(date(2026, 1, 31), 4)
    assert "Jan" in label and "Feb" in label
