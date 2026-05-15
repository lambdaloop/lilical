from __future__ import annotations

from datetime import date, datetime, time, timedelta


def multi_day_span(inst) -> tuple[date, date] | None:
    """Return (start_day, end_day_inclusive) if inst should render in the all-day band.

    Returns non-None for two cases:
    - True multi-day events (end_day > start_day after half-open midnight adjustment).
    - Midnight-to-midnight whole-day events (00:00 → 00:00 next day) that Graph stores
      with isAllDay=false. These collapse to a single visible day in the band. Mirrors
      the heuristic in backends/caldav.py:228.
    """
    try:
        t = datetime.fromisoformat(inst.dtstart_local).astimezone()
        et = datetime.fromisoformat(inst.dtend_local).astimezone()
    except (ValueError, TypeError):
        return None
    start_day = t.date()
    end_day = et.date()
    if end_day <= start_day:
        return None
    # Half-open: event ending at 00:00 of day N actually ends on day N-1.
    if et.time() == time.min:
        end_day = end_day - timedelta(days=1)
    if end_day > start_day:
        return start_day, end_day
    # After adjustment end_day == start_day: catches midnight-to-midnight whole-day events.  # noqa: E501
    if t.time() == time.min and et.time() == time.min:
        return start_day, start_day
    return None
