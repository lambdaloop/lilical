"""Tests for extractable logic in WeekView (no full rendering)."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ── helpers for _compute_week_placements tests ──────────────────────────────


def _inst(dtstart_local: str, dtend_local: str, uid: str = "uid", cal_id: str = "cal"):
    return type(
        "_Inst",
        (),
        {
            "dtstart_local": dtstart_local,
            "dtend_local": dtend_local,
            "all_day": 0,
            "calendar_id": cal_id,
            "uid": uid,
            "dtstart_utc": 0,
        },
    )()


def _event(summary: str = "Test"):
    return type("_Event", (), {"summary": summary, "location": None})()


def _placements_for(inst, week_start: date, px_per_hour: int = 60) -> dict:
    from lilical.ui.views.week import _compute_week_placements

    ev = _event()
    data = {
        "instances": [inst],
        "events": {id(inst): ev},
        "cal_color": {inst.calendar_id: None},
        "start": week_start,
        "day_count": 7,
        "completions": frozenset(),
    }
    result = _compute_week_placements(
        data, px_per_hour=px_per_hour, time_format="24h", col_w=100.0
    )
    return result["new_placements"]


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


# ── _compute_week_placements: cross-midnight rendering ──────────────────────


def test_midnight_ending_event_full_height(qapp) -> None:
    """8 PM → next-day 00:00 renders as a single 4-hour chip, not a sliver."""
    # Tuesday 20:00 → Wednesday 00:00 (half-open midnight end)
    inst = _inst("2026-04-21T20:00:00", "2026-04-22T00:00:00")
    week_start = date(2026, 4, 20)  # Mon
    placements = _placements_for(inst, week_start, px_per_hour=60)

    assert len(placements) == 1
    pl = next(iter(placements.values()))
    # 240 min × 60 px/h / 60 = 240 px tall
    assert pl["rect"].height() == pytest.approx(240.0)
    assert not pl["continues_left"]
    assert not pl["continues_right"]


def test_short_cross_midnight_splits_into_two_chips(qapp) -> None:
    """11 PM → 2 AM (3 h, crosses midnight) yields two chips with chevrons."""
    inst = _inst("2026-04-21T23:00:00", "2026-04-22T02:00:00")
    week_start = date(2026, 4, 20)  # Mon; event on Tue (offset 1) and Wed (offset 2)
    placements = _placements_for(inst, week_start, px_per_hour=60)

    assert len(placements) == 2
    by_offset = {key[-1]: pl for key, pl in placements.items()}
    # Day 1 chip: 23:00 → 24:00, 60 min tall, continues_right
    assert by_offset[1]["rect"].height() == pytest.approx(60.0)
    assert by_offset[1]["continues_right"]
    assert not by_offset[1]["continues_left"]
    assert by_offset[1]["show_time_prefix"]
    # Day 2 chip: 00:00 → 02:00, 120 min tall, continues_left
    assert by_offset[2]["rect"].height() == pytest.approx(120.0)
    assert by_offset[2]["continues_left"]
    assert not by_offset[2]["continues_right"]
    assert not by_offset[2]["show_time_prefix"]


def test_long_cross_midnight_goes_to_band(qapp) -> None:
    """8 PM → 10 AM next day (14 h) routes to the all-day band, not timed chips."""
    inst = _inst("2026-04-21T20:00:00", "2026-04-22T10:00:00")
    week_start = date(2026, 4, 20)
    placements = _placements_for(inst, week_start)

    # All placements for band events contain "band" in their key.
    for key in placements:
        assert "band" in key, f"Expected band key, got {key}"
