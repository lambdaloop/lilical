"""Unit tests for format_rrule_human."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def fmt():
    from lilical.ui.widgets.recurrence_editor import format_rrule_human

    return format_rrule_human


def test_daily(fmt):
    assert fmt("FREQ=DAILY") == "Daily"


def test_daily_interval(fmt):
    assert fmt("FREQ=DAILY;INTERVAL=3") == "Every 3 days"


def test_weekly(fmt):
    assert fmt("FREQ=WEEKLY") == "Weekly"


def test_weekly_interval(fmt):
    assert fmt("FREQ=WEEKLY;INTERVAL=2") == "Every 2 weeks"


def test_weekly_byday_single(fmt):
    assert fmt("FREQ=WEEKLY;BYDAY=MO") == "Weekly on Mon"


def test_weekly_byday_two(fmt):
    assert fmt("FREQ=WEEKLY;BYDAY=MO,WE") == "Weekly on Mon and Wed"


def test_weekly_byday_many(fmt):
    assert fmt("FREQ=WEEKLY;BYDAY=MO,WE,FR") == "Weekly on Mon, Wed, and Fri"


def test_weekly_interval_byday(fmt):
    assert fmt("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO") == "Every 2 weeks on Mon"


def test_monthly(fmt):
    assert fmt("FREQ=MONTHLY") == "Monthly"


def test_monthly_interval(fmt):
    assert fmt("FREQ=MONTHLY;INTERVAL=3") == "Every 3 months"


def test_yearly(fmt):
    assert fmt("FREQ=YEARLY") == "Yearly"


def test_count_singular(fmt):
    assert fmt("FREQ=DAILY;COUNT=1") == "Daily, 1 time"


def test_count_plural(fmt):
    assert fmt("FREQ=WEEKLY;COUNT=10") == "Weekly, 10 times"


def test_until(fmt):
    result = fmt("FREQ=MONTHLY;UNTIL=20260601T000000Z")
    assert result == "Monthly, until Jun 1, 2026"


def test_until_short_form(fmt):
    result = fmt("FREQ=DAILY;UNTIL=20261231")
    assert result == "Daily, until Dec 31, 2026"


def test_unknown_freq_falls_back(fmt):
    raw = "FREQ=HOURLY;INTERVAL=2"
    assert fmt(raw) == raw


def test_garbage_falls_back(fmt):
    raw = "not-a-valid-rrule"
    assert fmt(raw) == raw


def test_malformed_until_date_does_not_crash(fmt):
    """An invalid date in UNTIL (e.g. Feb 30) is silently ignored."""
    result = fmt("FREQ=DAILY;UNTIL=20260230T000000Z")
    assert "until" not in result
    assert "Daily" in result
