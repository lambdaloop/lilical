from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import icalendar
import recurring_ical_events  # type: ignore[reportMissingTypeStubs]

from lilical.models.event import Event
from lilical.storage.event_store import EventStore


def _dt_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RecurrenceExpander:
    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    def expand_for_storage(
        self,
        event: Event,
        window_start: datetime,
        window_end: datetime,
        overrides: list[Event] | None = None,
    ) -> list[dict[str, Any]]:
        # Fetch sibling overrides (only meaningful for events with an rrule).
        # Callers inside an open DB session should pass pre-fetched overrides
        # to avoid nested-session issues with SQLAlchemy.
        if overrides is None:
            overrides = (
                self._store.get_override_events(event.uid, event.calendar_id)
                if event.rrule
                else []
            )
        override_hash = ",".join(
            sorted(ov.recurrence_id.isoformat() for ov in overrides if ov.recurrence_id)
        )

        cache_key = (
            event.uid,
            event.dtstart.isoformat() if event.dtstart else "",
            event.dtend.isoformat() if event.dtend else "",
            event.rrule or "",
            window_start.isoformat(),
            window_end.isoformat(),
            override_hash,
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

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

        occurrences = recurring_ical_events.of(ical).between(window_start, window_end)

        # Build a set of override recurrence_ids in UTC for matching.
        override_rid_utc: set[datetime] = set()
        for ov in overrides:
            if ov.recurrence_id is not None:
                override_rid_utc.add(_dt_to_utc(ov.recurrence_id))

        results: list[dict[str, Any]] = []
        for occ in occurrences:
            occ_start = occ.get("DTSTART").dt
            if isinstance(occ_start, datetime):
                occ_utc = _dt_to_utc(occ_start)
                if occ_utc in override_rid_utc:
                    continue  # replaced by an override
            results.append(
                {
                    "uid": event.uid,
                    "calendar_id": event.calendar_id,
                    "dtstart": occ_start,
                    "dtend": occ.get("DTEND").dt,
                    "all_day": event.all_day,
                    "is_override": False,
                    "recurrence_id": "",
                }
            )

        # Append override instances that fall within the window.
        for ov in overrides:
            if ov.recurrence_id is None or ov.dtstart is None:
                continue
            # A cancelled override (e.g. a server single-occurrence deletion,
            # type:"exception" + isCancelled) must leave a clean hole: the base
            # rrule occurrence at this slot is already suppressed via
            # override_rid_utc above, so skipping the append yields an exdate.
            if ov.status == "CANCELLED":
                continue
            ov_start = ov.dtstart
            ov_end = ov.dtend or ov.dtstart
            ov_start_utc = _dt_to_utc(ov_start)  # type: ignore[reportUnnecessaryIsInstance]
            ov_end_utc = (
                _dt_to_utc(ov_end) if isinstance(ov_end, datetime) else ov_start_utc  # type: ignore[reportUnnecessaryIsInstance]
            )
            if ov_start_utc < _dt_to_utc(window_end) and ov_end_utc > _dt_to_utc(
                window_start
            ):
                results.append(
                    {
                        "uid": event.uid,
                        "calendar_id": event.calendar_id,
                        "dtstart": ov_start,
                        "dtend": ov_end,
                        "all_day": ov.all_day,
                        "is_override": True,
                        "recurrence_id": ov.recurrence_id.isoformat(),
                    }
                )

        self._cache[cache_key] = results
        return results
