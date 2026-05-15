"""Tests for ui/views/_multi_day.multi_day_span."""
from __future__ import annotations

from datetime import date

from lilical.ui.views._multi_day import multi_day_span


def _inst(dtstart_local: str, dtend_local: str):
    return type("_Inst", (), {
        "dtstart_local": dtstart_local,
        "dtend_local": dtend_local,
    })()


def test_same_day_returns_none() -> None:
    assert multi_day_span(_inst("2026-05-13T09:00:00", "2026-05-13T10:00:00")) is None


def test_multi_day_returns_range() -> None:
    result = multi_day_span(_inst("2026-05-13T09:00:00", "2026-05-15T10:00:00"))
    assert result == (date(2026, 5, 13), date(2026, 5, 15))


def test_midnight_adjustment() -> None:
    """Event ending at midnight of day N is adjusted to end on day N-1."""
    result = multi_day_span(_inst("2026-05-13T09:00:00", "2026-05-15T00:00:00"))
    assert result == (date(2026, 5, 13), date(2026, 5, 14))


def test_midnight_to_midnight_whole_day() -> None:
    """00:00 → 00:00 next day collapses to a single visible day."""
    result = multi_day_span(_inst("2026-05-13T00:00:00", "2026-05-14T00:00:00"))
    assert result == (date(2026, 5, 13), date(2026, 5, 13))


def test_parse_error_returns_none() -> None:
    assert multi_day_span(_inst("not-a-date", "2026-05-15T10:00:00")) is None
    assert multi_day_span(_inst("2026-05-13T09:00:00", None)) is None  # type: ignore[arg-type]


def test_non_midnight_adjusted_returns_none() -> None:
    """After midnight adjustment, if end_day == start_day but not
    midnight-to-midnight, returns None."""
    result = multi_day_span(_inst("2026-05-13T09:00:00", "2026-05-14T00:00:00"))
    assert result is None
