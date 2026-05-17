"""Tests for utils.timezone fallback chain."""

from __future__ import annotations

from datetime import timedelta, tzinfo
from unittest.mock import MagicMock, patch

from lilical.utils.timezone import local_iana_tz, local_zoneinfo


class _FixedOffset(tzinfo):
    """A fixed-offset timezone with no .key attribute (simulates a plain tzinfo)."""

    def utcoffset(self, dt):
        return timedelta(hours=0)

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return timedelta(0)


@patch("lilical.utils.timezone.datetime")
def test_local_iana_tz_uses_key(mock_dt) -> None:
    """When datetime.now().astimezone().tzinfo has a .key, it is returned."""
    from zoneinfo import ZoneInfo

    fake_tz = ZoneInfo("America/Chicago")
    fake_now = MagicMock()
    fake_now.tzinfo = fake_tz
    fake_now.astimezone.return_value = fake_now
    mock_dt.now.return_value = fake_now
    assert local_iana_tz() == "America/Chicago"


@patch("lilical.utils.timezone.datetime")
@patch("lilical.utils.timezone.open")
@patch("lilical.utils.timezone.os.readlink")
def test_local_iana_tz_falls_back_to_etc_localtime(
    mock_readlink, mock_open, mock_dt
) -> None:
    """When .key is absent, the fallback reads /etc/localtime symlink."""
    fake_now = MagicMock()
    fake_now.tzinfo = _FixedOffset()
    fake_now.astimezone.return_value = fake_now
    mock_dt.now.return_value = fake_now
    mock_open.side_effect = FileNotFoundError("no /etc/timezone")
    mock_readlink.return_value = "/usr/share/zoneinfo/Europe/Berlin"
    assert local_iana_tz() == "Europe/Berlin"


@patch("lilical.utils.timezone.datetime")
@patch("lilical.utils.timezone.open")
@patch("lilical.utils.timezone.os.readlink")
def test_local_iana_tz_falls_back_to_utc(mock_readlink, mock_open, mock_dt) -> None:
    """When all detection methods fail, return 'UTC'."""
    fake_now = MagicMock()
    fake_now.tzinfo = _FixedOffset()
    fake_now.astimezone.return_value = fake_now
    mock_dt.now.return_value = fake_now
    mock_open.side_effect = FileNotFoundError("no /etc/timezone")
    mock_readlink.side_effect = OSError("no /etc/localtime")
    assert local_iana_tz() == "UTC"


@patch("lilical.utils.timezone.local_iana_tz")
def test_local_zoneinfo_fallback(mock_iana) -> None:
    """local_zoneinfo falls back to UTC on ZoneInfoNotFoundError."""
    from zoneinfo import ZoneInfo

    mock_iana.return_value = "Invalid/Zone"
    result = local_zoneinfo()
    assert result == ZoneInfo("UTC")
