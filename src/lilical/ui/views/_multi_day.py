from __future__ import annotations

from datetime import date, datetime, time, timedelta

from lilical.utils.timezone import to_display

MULTI_DAY_BAND_MIN_HOURS = 12


def multi_day_span(inst) -> tuple[date, date] | None:
    """Return (start_day, end_day_inclusive) if inst should render in the all-day band.

    Routes to the band for:
    - Events crossing midnight whose duration >= MULTI_DAY_BAND_MIN_HOURS hours.
    - Midnight-to-midnight whole-day events (00:00 → 00:00 next day) that Graph stores
      with isAllDay=false. These collapse to a single visible day. Mirrors the heuristic
      in backends/caldav.py:228.

    Short cross-midnight events (< MULTI_DAY_BAND_MIN_HOURS h) return None so the
    timed renderer can split them across days with continuation indicators.
    """
    all_day = bool(getattr(inst, "all_day", 0))
    try:
        if all_day:
            # Both endpoints read as bare wall clock — see _inst_time. Converting
            # them under a foreign display zone breaks both heuristics below: the
            # half-open midnight adjustment stops firing (inflating spans by a
            # day) and the 00:00 -> 00:00 check fails, dropping all-day rows
            # through to the timed renderer.
            t = datetime.fromisoformat(inst.dtstart_local).replace(tzinfo=None)
            et = datetime.fromisoformat(inst.dtend_local).replace(tzinfo=None)
        else:
            t = to_display(datetime.fromisoformat(inst.dtstart_local))
            et = to_display(datetime.fromisoformat(inst.dtend_local))
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
        # Genuine multi-day crossing → band only if long enough.
        if (et - t) < timedelta(hours=MULTI_DAY_BAND_MIN_HOURS):
            return None
        return start_day, end_day
    # After adjustment end_day == start_day.
    # Midnight-to-midnight whole-day events (00:00 → 00:00 next day) → band.
    if t.time() == time.min and et.time() == time.min:
        return start_day, start_day
    # e.g. 8 PM → next-day 00:00: single-day timed chip.
    return None
