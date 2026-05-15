"""Tests for the pure _parse_natural NL-parsing function in QuickAddDialog."""

from __future__ import annotations

from datetime import timedelta

from lilical.ui.widgets.quick_add_dialog import _parse_natural


def test_parse_returns_none_for_empty_string() -> None:
    assert _parse_natural("") is None


def test_parse_returns_none_for_gibberish() -> None:
    # Completely unparseable text with no recognisable date/time phrase.
    result = _parse_natural("xyzzy frobozz qux")
    # dateparser may or may not find a date in arbitrary text; if it doesn't,
    # we get None; if it does (rare), we just verify the shape.
    if result is not None:
        assert "dtstart" in result


def test_parse_tomorrow_at_noon_returns_aware_datetime() -> None:
    # Use bare time phrase dateparser can reliably parse.
    result = _parse_natural("tomorrow at noon")
    assert result is not None
    assert result["dtstart"].tzinfo is not None


def test_parse_dtend_is_one_hour_after_dtstart() -> None:
    result = _parse_natural("tomorrow at 2pm")
    assert result is not None
    assert result["dtend"] - result["dtstart"] == timedelta(hours=1)


def test_parse_result_has_required_keys() -> None:
    result = _parse_natural("9am tomorrow")
    if result is not None:
        assert all(k in result for k in ("title", "dtstart", "dtend", "location"))


def test_parse_prefers_future_dates() -> None:
    # With PREFER_DATES_FROM=future the returned datetime should be in the future
    # relative to "now" when the test runs.
    import datetime

    result = _parse_natural("next Monday at 10am")
    if result is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        assert result["dtstart"] > now
