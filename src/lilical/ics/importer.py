from __future__ import annotations

import logging
import zoneinfo
from datetime import date as _date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import icalendar

from lilical.models.event import Event
from lilical.utils.timezone import local_iana_tz

log = logging.getLogger(__name__)


def _normalise_dt(val: object, tzid_hint: str | None = None) -> datetime | None:
    """Coerce an icalendar .dt value to a tz-aware datetime.

    - date → naive datetime at midnight (caller handles all-day localization)
    - aware datetime → returned as-is
    - naive datetime → tagged with TZID hint, falling back to UTC
    """
    if val is None:
        return None
    if isinstance(val, _date_cls) and not isinstance(val, datetime):
        return datetime.combine(val, time.min)
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val
        if tzid_hint and tzid_hint != "UTC":
            try:
                return val.replace(tzinfo=zoneinfo.ZoneInfo(tzid_hint))
            except Exception:
                log.debug("unknown TZID %r, falling back to UTC", tzid_hint)
        return val.replace(tzinfo=timezone.utc)
    return None


def _prop_dt(prop: object, tzid_hint: str | None = None) -> datetime | None:
    if prop is None:
        return None
    return _normalise_dt(getattr(prop, "dt", None), tzid_hint)


def _prop_dt_tuple(prop: object) -> tuple[datetime, ...]:
    """Flatten EXDATE/RDATE into a tuple of datetimes."""
    if prop is None:
        return ()
    items = prop if isinstance(prop, list) else [prop]
    out: list[datetime] = []
    for p in items:
        dts = getattr(p, "dts", None)
        if dts is not None:
            for entry in dts:
                normalised = _normalise_dt(getattr(entry, "dt", None))
                if normalised is not None:
                    out.append(normalised)
        else:
            normalised = _normalise_dt(getattr(p, "dt", None))
            if normalised is not None:
                out.append(normalised)
    return tuple(out)


def _ics_calendar_name(cal: icalendar.Calendar) -> str | None:
    """Extract X-WR-CALNAME if present; many publishers include it."""
    name = cal.get("X-WR-CALNAME")
    return str(name) if name else None


def _vevent_to_event(ve: icalendar.Event, *, calendar_id: str) -> Event | None:
    """Convert a single VEVENT to an Event. Returns None if UID is missing."""
    uid = str(ve.get("UID", "")).strip()
    if not uid:
        return None

    dtstart_prop = ve.get("DTSTART")
    if dtstart_prop is None:
        return None
    dtstart_raw = getattr(dtstart_prop, "dt", None)
    dtstart_params = getattr(dtstart_prop, "params", None) or {}

    all_day = (
        isinstance(dtstart_raw, _date_cls) and not isinstance(dtstart_raw, datetime)
    ) or str(dtstart_params.get("VALUE", "")).upper() == "DATE"
    tz = str(dtstart_params.get("TZID", "UTC")) if dtstart_params else "UTC"

    dtstart = _prop_dt(dtstart_prop, tz)

    dtend_prop = ve.get("DTEND")
    dtend = _prop_dt(dtend_prop, tz)
    if dtend is None and dtstart is not None:
        duration_prop = ve.get("DURATION")
        dur = getattr(duration_prop, "dt", None) if duration_prop else None
        if dur is not None:
            dtend = dtstart + dur
        elif all_day:
            dtend = dtstart + timedelta(days=1)
        else:
            dtend = dtstart

    rrule_prop = ve.get("RRULE")
    rrule: str | None = None
    if rrule_prop is not None:
        try:
            rrule = rrule_prop.to_ical().decode()
        except Exception:
            log.debug("failed to serialize RRULE for %s", uid)

    exdates = _prop_dt_tuple(ve.get("EXDATE"))
    rdates = _prop_dt_tuple(ve.get("RDATE"))

    rid_prop = ve.get("RECURRENCE-ID")
    recurrence_id = _prop_dt(rid_prop, tz) if rid_prop is not None else None

    # Re-localize bare-UTC events into local zone so the user sees a sensible
    # wall-clock display (same instant, just a friendlier representation).
    if tz == "UTC" and not all_day and dtstart is not None:
        local_name = local_iana_tz()
        if local_name != "UTC":
            try:
                local_zone = zoneinfo.ZoneInfo(local_name)
                dtstart = dtstart.astimezone(local_zone)
                if dtend is not None:
                    dtend = dtend.astimezone(local_zone)
                tz = local_name
            except Exception:
                pass

    # All-day events anchored at local-zone midnight so .date() returns the
    # intended calendar day regardless of source encoding.
    if all_day and dtstart is not None:
        try:
            local_zone = zoneinfo.ZoneInfo(local_iana_tz())
            dtstart = datetime.combine(dtstart.date(), time.min, tzinfo=local_zone)
            if dtend is not None:
                dtend = datetime.combine(dtend.date(), time.min, tzinfo=local_zone)
            tz = local_zone.key
        except Exception:
            pass

    categories_raw = ve.get("CATEGORIES")
    if categories_raw is None:
        categories: tuple[str, ...] = ()
    else:
        items = categories_raw if isinstance(categories_raw, list) else [categories_raw]
        flat: list[str] = []
        for it in items:
            cats = getattr(it, "cats", None)
            if cats is not None:
                flat.extend(str(c) for c in cats)
            else:
                flat.append(str(it))
        categories = tuple(flat)

    url_prop = ve.get("URL")
    url = str(url_prop) if url_prop is not None else None

    last_modified = _prop_dt(ve.get("LAST-MODIFIED"))

    try:
        sequence = int(ve.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        sequence = 0

    return Event(
        uid=uid,
        calendar_id=calendar_id,
        recurrence_id=recurrence_id,
        provider_event_id=uid,
        dtstart=dtstart,
        dtend=dtend,
        tz=tz,
        all_day=all_day,
        summary=str(ve.get("SUMMARY", "")),
        description=str(ve.get("DESCRIPTION", "")),
        location=str(ve.get("LOCATION", "")),
        url=url,
        rrule=rrule,
        exdates=exdates,
        rdates=rdates,
        categories=categories,
        status=str(ve.get("STATUS", "CONFIRMED")),
        transparency=str(ve.get("TRANSP", "OPAQUE")),
        last_modified=last_modified,
        sequence=sequence,
    )


def parse_ics_to_events(
    ics_bytes: bytes, calendar_id: str
) -> tuple[list[Event], str | None]:
    """Parse a VCALENDAR blob to events.

    Returns (events, x_wr_calname). x_wr_calname is the publisher-suggested
    display name for the feed, or None if absent.
    """
    text = ics_bytes.decode("utf-8", errors="replace")
    cal = icalendar.Calendar.from_ical(text)
    events: list[Event] = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        try:
            ev = _vevent_to_event(component, calendar_id=calendar_id)  # type: ignore[arg-type]
        except Exception:
            log.exception("failed to parse VEVENT, skipping")
            continue
        if ev is not None:
            events.append(ev)
    return events, _ics_calendar_name(cal)  # type: ignore[arg-type]


def parse_ics_file(path: str | Path) -> list[Event]:
    """Back-compat shim: parse a file on disk and return events only."""
    events, _ = parse_ics_to_events(Path(path).read_bytes(), calendar_id="")
    return events
