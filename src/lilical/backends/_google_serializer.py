"""Google Calendar API serialization helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lilical.models.event import Event

# Google's colorId values don't map cleanly to arbitrary hex.
# We map our named color hexes to the nearest Google Calendar colorId.
_HEX_TO_COLOR_ID: dict[str, str] = {
    "#e05050": "11",  # Tomato
    "#e07878": "4",  # Flamingo
    "#e08030": "6",  # Tangerine
    "#e0c830": "5",  # Banana
    "#70a870": "2",  # Sage
    "#3a7a3a": "10",  # Basil
    "#3a80c8": "7",  # Peacock
    "#3a50b8": "9",  # Blueberry
    "#9a78e0": "1",  # Lavender
    "#7a3aaa": "3",  # Grape
    "#8a8a8a": "8",  # Graphite
    "#3ab8c8": "7",  # Cyan → Peacock (closest)
}


def event_to_google_body(event: "Event") -> dict[str, Any]:
    """Serialize an Event to a Google Calendar API request body."""
    body: dict[str, Any] = {
        "summary": event.summary or "",
    }

    if event.description:
        body["description"] = event.description
    if event.location:
        body["location"] = event.location

    if event.dtstart is not None:
        body["start"] = _dt_to_google(event.dtstart, event.tz, event.all_day)
    if event.dtend is not None:
        body["end"] = _dt_to_google(event.dtend, event.tz, event.all_day)

    if event.all_day:
        body["start"] = _dt_to_google(event.dtstart, event.tz, all_day=True)
        body["end"] = _dt_to_google(event.dtend, event.tz, all_day=True)

    if event.status:
        body["status"] = event.status.lower()

    transparency = event.transparency or "OPAQUE"
    body["transparency"] = transparency.lower()

    if event.color and event.color in _HEX_TO_COLOR_ID:
        body["colorId"] = _HEX_TO_COLOR_ID[event.color]

    if event.url:
        body["source"] = {"url": event.url, "title": ""}

    recurrence_lines: list[str] = []
    if event.rrule:
        recurrence_lines.append(f"RRULE:{event.rrule}")
    for exdate in event.exdates:
        exdate_str = _format_exdate(exdate, event.tz)
        recurrence_lines.append(exdate_str)
    for rdate in event.rdates:
        recurrence_lines.append(f"RDATE:{rdate.strftime('%Y%m%dT%H%M%SZ')}")
    if recurrence_lines:
        body["recurrence"] = recurrence_lines

    if event.recurrence_id is not None:
        body["recurringEventId"] = event.uid

    if event.attendees:
        body["attendees"] = [
            {
                "email": att.email,
                **({"displayName": att.display_name} if att.display_name else {}),
            }
            for att in event.attendees
        ]

    return body


def _dt_to_google(
    dt: datetime | None, tz_name: str | None, all_day: bool
) -> dict[str, str]:
    if dt is None:
        return {}
    if all_day:
        return {"date": dt.strftime("%Y-%m-%d")}
    tz = tz_name or "UTC"
    return {"dateTime": dt.isoformat(), "timeZone": tz}


def _format_exdate(dt: datetime, tz_name: str | None) -> str:
    if tz_name and tz_name != "UTC":
        return f"EXDATE;TZID={tz_name}:{dt.strftime('%Y%m%dT%H%M%S')}"
    utc = dt.astimezone(timezone.utc)
    return f"EXDATE:{utc.strftime('%Y%m%dT%H%M%SZ')}"
