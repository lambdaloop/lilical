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
    # EXDATE/RDATE/RECURRENCE-ID value types must match DTSTART (RFC 5545):
    # DATE for all-day series, DATE-TIME otherwise. A mismatched type is
    # ignored by many servers, so the exclusion/override silently fails.
    for exdate in event.exdates:
        if event.all_day:
            ve.add("exdate", exdate.date(), parameters={"VALUE": "DATE"})
        else:
            ve.add("exdate", exdate)
    for rdate in event.rdates:
        if event.all_day:
            ve.add("rdate", rdate.date(), parameters={"VALUE": "DATE"})
        else:
            ve.add("rdate", rdate)
    if event.recurrence_id is not None:
        if event.all_day:
            ve.add("recurrence-id", event.recurrence_id.date())
        else:
            _add_dt_with_tzid(ve, "recurrence-id", event.recurrence_id, event.tz)

    for att in event.attendees:
        attendee_val = icalendar.vCalAddress(f"mailto:{att.email}")
        attendee_val.params["ROLE"] = "REQ-PARTICIPANT"
        attendee_val.params["PARTSTAT"] = att.response
        attendee_val.params["RSVP"] = "TRUE"
        if att.display_name:
            attendee_val.params["CN"] = att.display_name
        ve.add("attendee", attendee_val)

    if event.organizer:
        org_val = icalendar.vCalAddress(f"mailto:{event.organizer.email}")
        if event.organizer.display_name:
            org_val.params["CN"] = event.organizer.display_name
        ve.add("organizer", org_val)

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
