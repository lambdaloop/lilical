"""iCalendar serialization helpers for the CalDAV backend."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import icalendar

if TYPE_CHECKING:
    from lilical.models.event import Event


def event_to_vcalendar(
    event: "Event", *, sequence_bump: bool = False
) -> icalendar.Calendar:
    """Serialize an Event to a VCALENDAR with a single VEVENT component."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//lilical//lilical//EN")
    cal.add("version", "2.0")

    ve = icalendar.Event()
    ve.add("uid", event.uid)
    ve.add("dtstamp", datetime.now(timezone.utc))
    ve.add("summary", event.summary or "")

    seq = (event.sequence or 0) + (1 if sequence_bump else 0)
    ve.add("sequence", seq)

    if event.dtstart is not None:
        if event.all_day:
            ve.add("dtstart", event.dtstart.date())
        else:
            _add_dt_with_tzid(ve, "dtstart", event.dtstart, event.tz)

    if event.dtend is not None:
        if event.all_day:
            ve.add("dtend", event.dtend.date())
        else:
            _add_dt_with_tzid(ve, "dtend", event.dtend, event.tz)

    if event.description:
        ve.add("description", event.description)
    if event.location:
        ve.add("location", event.location)
    if event.url:
        ve.add("url", event.url)
    if event.status:
        ve.add("status", event.status)
    transp = "TRANSPARENT" if event.transparency == "TRANSPARENT" else "OPAQUE"
    ve.add("transp", transp)
    if event.color:
        ve.add("color", event.color)
    if event.rrule:
        # icalendar can parse RRULE strings directly
        ve.add("rrule", icalendar.vRecur.from_ical(event.rrule))
    for exdate in event.exdates:
        ve.add("exdate", exdate)
    for rdate in event.rdates:
        ve.add("rdate", rdate)
    if event.recurrence_id is not None:
        _add_dt_with_tzid(ve, "recurrence-id", event.recurrence_id, event.tz)

    cal.add_component(ve)
    return cal


def _add_dt_with_tzid(
    component: icalendar.Component,
    prop_name: str,
    dt: datetime,
    tz_name: str | None,
) -> None:
    """Add a datetime property with proper TZID parameter when not UTC."""
    import zoneinfo

    if dt.tzinfo is timezone.utc or tz_name in (None, "", "UTC"):
        component.add(prop_name, dt.astimezone(timezone.utc))
        return
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        local_dt = dt.astimezone(tz)
        prop = icalendar.vDatetime(local_dt)
        prop.params["TZID"] = tz_name
        component.add(prop_name, prop)
    except Exception:
        component.add(prop_name, dt.astimezone(timezone.utc))
