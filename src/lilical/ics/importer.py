from __future__ import annotations

import logging
from pathlib import Path

import icalendar

from lilical.models.event import Event

log = logging.getLogger(__name__)


def parse_ics_file(path: str | Path) -> list[Event]:
    content = Path(path).read_text()
    cal = icalendar.Calendar.from_ical(content)
    events: list[Event] = []
    for component in cal.walk():
        if component.name == "VEVENT":
            uid = str(component.get("UID", ""))
            summary = str(component.get("SUMMARY", ""))
            events.append(Event(
                uid=uid,
                calendar_id="",
                summary=summary,
            ))
    return events
