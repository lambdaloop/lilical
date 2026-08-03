"""Tests for _compute_day_placements cross-midnight rendering."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

from lilical.utils.timezone import QUERY_PAD

from .conftest import display_tz

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _inst(
    dtstart_local: str,
    dtend_local: str,
    uid: str = "uid",
    cal_id: str = "cal",
    all_day: int = 0,
):
    return type(
        "_Inst",
        (),
        {
            "dtstart_local": dtstart_local,
            "dtend_local": dtend_local,
            "all_day": all_day,
            "calendar_id": cal_id,
            "uid": uid,
            "dtstart_utc": 0,
        },
    )()


def _event(summary: str = "Test"):
    return type("_Event", (), {"summary": summary, "location": None})()


def _placements_for(inst, day: date, px_per_hour: int = 60) -> dict:
    from lilical.ui.views.day import _compute_day_placements

    ev = _event()
    data = {
        "instances": [inst],
        "events": {id(inst): ev},
        "cal_color": {inst.calendar_id: None},
        "day": day,
        "completions": frozenset(),
    }
    result = _compute_day_placements(
        data, col_w=300.0, px_per_hour=px_per_hour, time_format="24h"
    )
    return result["new_placements"]


def test_midnight_ending_event_full_height(qapp) -> None:
    """8 PM → next-day 00:00 renders as a single 4-hour chip on the start day."""
    inst = _inst("2026-04-21T20:00:00", "2026-04-22T00:00:00")
    day = date(2026, 4, 21)
    placements = _placements_for(inst, day, px_per_hour=60)

    assert len(placements) == 1
    pl = next(iter(placements.values()))
    assert pl["rect"].height() == pytest.approx(240.0)
    assert not pl["continues_left"]
    assert not pl["continues_right"]


def test_midnight_ending_not_on_next_day(qapp) -> None:
    """8 PM → next-day 00:00 produces no chip when viewing the next day."""
    inst = _inst("2026-04-21T20:00:00", "2026-04-22T00:00:00")
    placements = _placements_for(inst, date(2026, 4, 22))
    assert len(placements) == 0


def test_short_cross_midnight_start_day(qapp) -> None:
    """11 PM → 2 AM: start-day chip goes to 24:00 with continues_right."""
    inst = _inst("2026-04-21T23:00:00", "2026-04-22T02:00:00")
    placements = _placements_for(inst, date(2026, 4, 21), px_per_hour=60)

    assert len(placements) == 1
    pl = next(iter(placements.values()))
    assert pl["rect"].height() == pytest.approx(60.0)  # 60 min × 60 px/h / 60
    assert pl["continues_right"]
    assert not pl["continues_left"]
    assert pl["show_time_prefix"]


def test_short_cross_midnight_end_day(qapp) -> None:
    """11 PM → 2 AM: end-day chip from 00:00 with continues_left."""
    inst = _inst("2026-04-21T23:00:00", "2026-04-22T02:00:00")
    placements = _placements_for(inst, date(2026, 4, 22), px_per_hour=60)

    assert len(placements) == 1
    pl = next(iter(placements.values()))
    assert pl["rect"].height() == pytest.approx(120.0)  # 120 min × 60 px/h / 60
    assert pl["continues_left"]
    assert not pl["continues_right"]
    assert not pl["show_time_prefix"]


def test_long_cross_midnight_goes_to_band(qapp) -> None:
    """8 PM → 10 AM next day (14 h) routes to the band, not the timed grid."""
    inst = _inst("2026-04-21T20:00:00", "2026-04-22T10:00:00")
    day = date(2026, 4, 21)
    placements = _placements_for(inst, day)

    for key in placements:
        assert "band" in key, f"Expected band key, got {key}"


# ── Display timezone ────────────────────────────────────────────────────────


def test_timed_placement_uses_display_tz(qapp) -> None:
    """The same instant renders on different days depending on the zone."""
    # 2026-04-21 23:00 -07:00 == 2026-04-22 15:00 in Tokyo.
    inst = _inst("2026-04-21T23:00:00-07:00", "2026-04-21T23:30:00-07:00")

    with display_tz("America/Los_Angeles"):
        assert _placements_for(inst, date(2026, 4, 21))
        assert not _placements_for(inst, date(2026, 4, 22))

    with display_tz("Asia/Tokyo"):
        assert not _placements_for(inst, date(2026, 4, 21))
        assert _placements_for(inst, date(2026, 4, 22))


def test_all_day_row_stable_across_display_tz(qapp) -> None:
    """An all-day event stays on its own date in every display zone."""
    inst = _inst("2026-04-21T00:00:00+09:00", "2026-04-22T00:00:00+09:00", all_day=1)

    for zone in ("Asia/Tokyo", "America/Los_Angeles", "Etc/GMT+12"):
        with display_tz(zone):
            assert _placements_for(inst, date(2026, 4, 21)), f"missing in {zone}"
            assert not _placements_for(inst, date(2026, 4, 20)), f"leaked in {zone}"
            assert not _placements_for(inst, date(2026, 4, 22)), f"leaked in {zone}"


def test_query_window_is_padded(qapp) -> None:
    from lilical.ui.views.day import _query_day_data

    captured = {}

    class _Store:
        def list_instances(self, start, end, calendar_ids=None):
            captured["start"] = start
            captured["end"] = end
            return []

        def events_for_instances(self, _insts):
            return {}

        def completion_for_instances(self, _insts):
            return frozenset()

    _query_day_data(_Store(), date(2026, 4, 21), {})
    span = captured["end"] - captured["start"]
    assert span >= timedelta(hours=28) + 2 * QUERY_PAD
