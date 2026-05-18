from __future__ import annotations

from datetime import date, timedelta

WEEK_START_NAMES = ("monday", "sunday", "saturday")

# Mon-based index (0=Mon…6=Sun) of the first day for each week-start name.
_START_DOW: dict[str, int] = {"monday": 0, "sunday": 6, "saturday": 5}

_LABELS_LONG = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_LABELS_SHORT = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def start_of_week(d: date, week_start: str) -> date:
    """Return the first day of the week containing *d*."""
    start_dow = _START_DOW.get(week_start, 0)
    return d - timedelta(days=(d.weekday() - start_dow) % 7)


def dow_labels(week_start: str) -> list[str]:
    """Full day-of-week labels starting from the configured first day."""
    i = _START_DOW.get(week_start, 0)
    return _LABELS_LONG[i:] + _LABELS_LONG[:i]


def dow_labels_short(week_start: str) -> list[str]:
    """Abbreviated day-of-week labels starting from the configured first day."""
    i = _START_DOW.get(week_start, 0)
    return _LABELS_SHORT[i:] + _LABELS_SHORT[:i]


def weekend_columns(week_start: str) -> frozenset[int]:
    """Column indices (0-based) of Sat and Sun for the given week-start."""
    start_dow = _START_DOW.get(week_start, 0)
    sat_col = (5 - start_dow) % 7
    sun_col = (6 - start_dow) % 7
    return frozenset({sat_col, sun_col})
