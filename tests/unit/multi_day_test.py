"""Tests for ui/views/_multi_day.multi_day_span."""

from __future__ import annotations

from datetime import date

from lilical.ui.views._multi_day import multi_day_span


def _inst(dtstart_local: str, dtend_local: str):
    return type(
        "_Inst",
        (),
        {
            "dtstart_local": dtstart_local,
            "dtend_local": dtend_local,
        },
    )()


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


def test_all_day_multi_day_span() -> None:
    """All-day event spanning 3 days (dtend exclusive midnight) → inclusive range."""
    result = multi_day_span(_inst("2026-05-13T00:00:00", "2026-05-16T00:00:00"))
    assert result == (date(2026, 5, 13), date(2026, 5, 15))


def test_short_crossing_returns_none() -> None:
    """Cross-midnight event under 12 h stays in the timed grid."""
    result = multi_day_span(_inst("2026-04-21T22:00:00", "2026-04-22T06:00:00"))
    assert result is None


def test_long_crossing_returns_range() -> None:
    """Cross-midnight event >= 12 h routes to the all-day band."""
    result = multi_day_span(_inst("2026-04-21T20:00:00", "2026-04-22T12:00:00"))
    assert result == (date(2026, 4, 21), date(2026, 4, 22))


def test_threshold_boundary_midnight_ending() -> None:
    """Exactly 12 h but ending at midnight → half-open → single-day timed."""
    result = multi_day_span(_inst("2026-04-21T12:00:00", "2026-04-22T00:00:00"))
    assert result is None


def test_threshold_boundary_12h_non_midnight() -> None:
    """Exactly 12 h crossing NOT ending at midnight → band."""
    result = multi_day_span(_inst("2026-04-21T14:00:00", "2026-04-22T02:00:00"))
    assert result == (date(2026, 4, 21), date(2026, 4, 22))
