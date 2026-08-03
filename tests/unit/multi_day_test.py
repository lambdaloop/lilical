"""Tests for ui/views/_multi_day.multi_day_span."""

from __future__ import annotations

from datetime import date

from lilical.ui.views._multi_day import multi_day_span

from .conftest import display_tz


def _inst(dtstart_local: str, dtend_local: str, all_day: int = 0):
    return type(
        "_Inst",
        (),
        {
            "dtstart_local": dtstart_local,
            "dtend_local": dtend_local,
            "all_day": all_day,
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


# ── Display-zone behaviour ────────────────────────────────────────────────


def test_all_day_span_uses_wall_clock_under_foreign_display_tz() -> None:
    """All-day rows keep their dates whatever zone you view them from.

    They are anchored at system-local midnight, so converting them would both
    inflate the span (the half-open midnight adjustment stops firing) and drop
    them out of the band entirely.
    """
    inst = _inst("2026-05-13T00:00:00+09:00", "2026-05-16T00:00:00+09:00", all_day=1)
    for zone in ("Asia/Tokyo", "America/Los_Angeles", "Etc/GMT+12"):
        with display_tz(zone):
            assert multi_day_span(inst) == (date(2026, 5, 13), date(2026, 5, 15))


def test_single_all_day_stays_in_band_under_foreign_display_tz() -> None:
    inst = _inst("2026-05-13T00:00:00+09:00", "2026-05-14T00:00:00+09:00", all_day=1)
    with display_tz("America/Los_Angeles"):
        assert multi_day_span(inst) == (date(2026, 5, 13), date(2026, 5, 13))


def test_timed_midnight_to_midnight_becomes_two_day_span_under_foreign_tz() -> None:
    """Pins an accepted behaviour change, so nobody "fixes" it later.

    A Graph isAllDay=false midnight-to-midnight block is a whole day only in its
    own zone. Viewed from 16 h away it genuinely straddles two days, so it lands
    in the band as a 2-day span rather than collapsing to one.
    """
    inst = _inst("2026-05-13T00:00:00+09:00", "2026-05-14T00:00:00+09:00")
    with display_tz("Asia/Tokyo"):
        assert multi_day_span(inst) == (date(2026, 5, 13), date(2026, 5, 13))
    with display_tz("America/Los_Angeles"):
        assert multi_day_span(inst) == (date(2026, 5, 12), date(2026, 5, 13))
