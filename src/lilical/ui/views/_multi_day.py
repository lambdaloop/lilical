from __future__ import annotations

from datetime import date, datetime, time, timedelta


def multi_day_span(inst) -> tuple[date, date] | None:
    """Return (start_day, end_day_inclusive) if inst spans >1 calendar day, else None.

    Mirrors the midnight-end adjustment in month.py: an event ending at exactly
    00:00 of day N is treated as ending on day N-1 (half-open interval).
    """
    try:
        t = datetime.fromisoformat(inst.dtstart_local).astimezone()
        et = datetime.fromisoformat(inst.dtend_local).astimezone()
    except (ValueError, TypeError):
        return None
    start_day = t.date()
    end_day = et.date()
    if et.time() == time.min and end_day > start_day:
        end_day = end_day - timedelta(days=1)
    if end_day > start_day:
        return start_day, end_day
    return None
