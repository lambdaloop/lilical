from __future__ import annotations

from datetime import datetime
from typing import Any

import icalendar
import recurring_ical_events

from lilical.models.event import Event
from lilical.storage.event_store import EventStore


class RecurrenceExpander:
    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._cache: dict[tuple, list[dict[str, Any]]] = {}

    def expand_for_storage(
        self,
        event: Event,
        window_start: datetime,
        window_end: datetime,
    ) -> list[dict[str, Any]]:
        ical = icalendar.Calendar()
        ical.add("PRODID", "-//lilical//lilical//EN")
        ical.add("VERSION", "2.0")
        ve = icalendar.Event()
        ve.add("UID", event.uid)
        if event.rrule:
            ve.add("RRULE", icalendar.vRecur.from_ical(event.rrule))
        if event.dtstart:
            ve.add("DTSTART", event.dtstart)
        if event.dtend:
            ve.add("DTEND", event.dtend)
        if event.exdates:
            for ed in event.exdates:
                ve.add("EXDATE", ed)
        if event.rdates:
            for rd in event.rdates:
                ve.add("RDATE", rd)
        ical.add_component(ve)

        cache_key = (event.uid, event.etag or "", window_start.isoformat(), window_end.isoformat())
        if cache_key in self._cache:
            return self._cache[cache_key]

        occurrences = recurring_ical_events.of(ical).between(window_start, window_end)
        results = [
            {
                "uid": event.uid,
                "calendar_id": event.calendar_id,
                "dtstart": occ.get("DTSTART").dt,
                "dtend": occ.get("DTEND").dt,
                "all_day": event.all_day,
                "is_override": False,
            }
            for occ in occurrences
        ]
        self._cache[cache_key] = results
        return results
