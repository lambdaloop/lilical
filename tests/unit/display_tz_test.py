"""Tests for the app-wide display zone in utils.timezone."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from lilical.utils.timezone import (
    QUERY_PAD,
    display_midnight,
    display_now,
    display_today,
    display_tz_name,
    display_zone,
    iana_zones,
    local_iana_tz,
    set_display_tz,
    to_display,
    zone_exists,
)

_LA = "America/Los_Angeles"
_TOKYO = "Asia/Tokyo"


def test_default_display_tz_is_system_zone() -> None:
    """Before anything sets it, the display zone is the OS zone."""
    # The autouse fixture restores this, so read it via a fresh import path.
    assert display_tz_name() == local_iana_tz()


def test_set_display_tz_updates_name_and_zone() -> None:
    assert set_display_tz(_TOKYO) is True
    assert display_tz_name() == _TOKYO
    assert display_zone() == ZoneInfo(_TOKYO)


def test_set_display_tz_rejects_unknown_name_and_keeps_previous() -> None:
    set_display_tz(_LA)
    assert set_display_tz("Not/AZone") is False
    assert display_tz_name() == _LA
    assert display_zone() == ZoneInfo(_LA)


def test_to_display_converts_aware_datetime() -> None:
    set_display_tz(_TOKYO)
    dt = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
    out = to_display(dt)
    assert out.hour == 9
    assert out.date() == date(2026, 5, 13)
    # Same instant, different spelling.
    assert out == dt


def test_to_display_treats_naive_as_display_zone() -> None:
    """Deliberately unlike a bare .astimezone(), which reads naive as OS-local."""
    set_display_tz(_TOKYO)
    out = to_display(datetime(2026, 5, 13, 9, 0))
    assert out.tzinfo == ZoneInfo(_TOKYO)
    assert out.hour == 9
    assert out.utcoffset() == timedelta(hours=9)


def test_display_midnight_is_midnight_in_display_zone() -> None:
    set_display_tz(_LA)
    out = display_midnight(date(2026, 5, 13))
    assert (out.hour, out.minute, out.second) == (0, 0, 0)
    assert out.utcoffset() == timedelta(hours=-7)  # PDT
    assert out.date() == date(2026, 5, 13)


def test_display_today_follows_zone() -> None:
    """An instant near a date boundary lands on different dates per zone."""
    set_display_tz(_LA)
    la = display_today()
    set_display_tz(_TOKYO)
    tokyo = display_today()
    # Tokyo is always at or ahead of LA; they differ for 16-17 h of each day.
    assert tokyo >= la
    assert (tokyo - la).days <= 1


def test_display_now_is_aware_and_in_display_zone() -> None:
    set_display_tz(_TOKYO)
    now = display_now()
    assert now.tzinfo == ZoneInfo(_TOKYO)


def test_iana_zones_is_sorted_and_cached() -> None:
    first = iana_zones()
    assert list(first) == sorted(first)
    assert "America/Los_Angeles" in first
    assert iana_zones() is first


def test_zone_exists_rejects_garbage() -> None:
    assert zone_exists(_LA) is True
    assert zone_exists("UTC") is True
    assert zone_exists("Not/AZone") is False
    assert zone_exists("") is False


def test_query_pad_covers_the_iana_offset_spread() -> None:
    """-12..+14 is a 26 h spread; the pad must exceed it."""
    assert timedelta(hours=26) < QUERY_PAD
