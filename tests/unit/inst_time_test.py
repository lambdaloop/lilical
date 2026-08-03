"""Tests for _inst_time — display conversion and the recurrence-identity invariant.

The invariant is the important part: `instance_dtstart` flows from
`_compute_*_placements` all the way into `EventStore.queue_update_instance` /
`queue_split_series`, where `recurrence.identity.recurrence_key` reduces it to
the integer that names one slot in a series. If a display-zone change moved that
key, dragging one occurrence of a series would create duplicate override rows
instead of updating the existing one.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from lilical.recurrence.identity import recurrence_key
from lilical.ui.views._inst_time import inst_end, inst_start

from .conftest import display_tz

_ZONES = ["America/Los_Angeles", "Asia/Tokyo", "Etc/GMT+12"]


def _inst(dtstart_local: str, dtend_local: str, *, all_day: int = 0):
    return SimpleNamespace(
        dtstart_local=dtstart_local, dtend_local=dtend_local, all_day=all_day
    )


def test_inst_start_preserves_instant_for_timed() -> None:
    """Timed slots key on the UTC epoch-minute — respelling the offset is a no-op."""
    inst = _inst("2026-05-13T09:00:00+02:00", "2026-05-13T10:00:00+02:00")
    keys = set()
    for zone in _ZONES:
        with display_tz(zone):
            start = inst_start(inst)
            assert start is not None
            assert start.tzinfo is not None
            keys.add(recurrence_key(start))
    assert len(keys) == 1


def test_inst_start_preserves_date_for_all_day() -> None:
    """All-day slots key on the wall-clock date, which .replace() leaves alone."""
    inst = _inst("2026-05-13T00:00:00+09:00", "2026-05-14T00:00:00+09:00", all_day=1)
    keys = set()
    for zone in _ZONES:
        with display_tz(zone):
            start = inst_start(inst)
            assert start is not None
            assert start.date() == date(2026, 5, 13)
            assert (start.hour, start.minute) == (0, 0)
            keys.add(recurrence_key(start, all_day=True))
    assert len(keys) == 1


def test_timed_instance_follows_display_zone() -> None:
    """The whole point: the rendered wall clock does move."""
    inst = _inst("2026-05-13T00:00:00+00:00", "2026-05-13T01:00:00+00:00")
    with display_tz("Asia/Tokyo"):
        start = inst_start(inst)
        assert start is not None
        assert start.hour == 9
    with display_tz("America/Los_Angeles"):
        start = inst_start(inst)
        assert start is not None
        assert start.hour == 17
        assert start.date() == date(2026, 5, 12)


def test_all_day_instance_does_not_follow_display_zone() -> None:
    inst = _inst("2026-05-13T00:00:00+09:00", "2026-05-14T00:00:00+09:00", all_day=1)
    with display_tz("America/Los_Angeles"):
        start = inst_start(inst)
        end = inst_end(inst)
        assert start is not None and end is not None
        assert start.date() == date(2026, 5, 13)
        assert end.date() == date(2026, 5, 14)


def test_inst_end_matches_inst_start_semantics() -> None:
    inst = _inst("2026-05-13T09:00:00+02:00", "2026-05-13T17:30:00+02:00")
    with display_tz("Asia/Tokyo"):
        end = inst_end(inst)
        assert end is not None
        assert (end.hour, end.minute) == (0, 30)
        assert end.date() == date(2026, 5, 14)


@pytest.mark.parametrize("bad", ["", "not-a-date", None])
def test_inst_start_returns_none_on_garbage(bad) -> None:
    """Call sites rely on None to take their existing fallback branch."""
    inst = _inst(bad, bad)
    assert inst_start(inst) is None
    assert inst_end(inst) is None


def test_missing_all_day_attribute_is_treated_as_timed() -> None:
    """Test fakes and older rows may not carry the flag."""
    inst = SimpleNamespace(
        dtstart_local="2026-05-13T00:00:00+00:00",
        dtend_local="2026-05-13T01:00:00+00:00",
    )
    with display_tz("Asia/Tokyo"):
        start = inst_start(inst)
        assert start is not None
        assert start.hour == 9
