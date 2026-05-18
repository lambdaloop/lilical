from __future__ import annotations


def fmt_hm(h: int, m: int, time_format: str) -> str:
    """Format hour + minute as 'HH:MM' (24h) or 'H:MM AM/PM' (12h)."""
    if time_format == "12h":
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ampm}"
    return f"{h:02d}:{m:02d}"


def fmt_hour_label(hour: int, time_format: str) -> str:
    """Format an hour (0-23) for the time-axis margin."""
    if time_format == "12h":
        if hour == 0:
            return "12 AM"
        if hour == 12:
            return "12 PM"
        return f"{hour - 12} PM" if hour > 12 else f"{hour} AM"
    return f"{hour:02d}:00"
