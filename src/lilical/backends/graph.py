from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import re
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, cast

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    EventChange,
    PermanentError,
    SyncCursor,
    TransientError,
)
from lilical.models.contact import Contact
from lilical.models.event import Attendee, Event, Organizer
from lilical.utils.timezone import local_iana_tz, local_zoneinfo

log = logging.getLogger(__name__)

# Evolution's public Microsoft 365 client ID. We piggy-back on it because (a) it's
# a registered, multi-tenant public client, (b) many corporate tenants that block
# generic "unknown app" consent already allow Evolution, and (c) Thunderbird's
# client ID hits "needs admin approval" in stricter tenants where Evolution does
# not. Trade-off: the user-facing consent screen reads "Evolution / GNOME".
GRAPH_CLIENT_ID = "20460e5d-ce91-49af-a3a5-70b6be7486d1"
GRAPH_AUTHORITY = "https://login.microsoftonline.com/common"
# The Evolution app registration only lists this redirect URI, not http://localhost.
GRAPH_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"
GRAPH_BASE_SCOPES = ["Calendars.ReadWrite", "User.Read"]
GRAPH_CONTACT_SCOPES = ["People.Read", "Contacts.Read", "User.ReadBasic.All"]
# Keep for backward compat with tests that import the name directly.
GRAPH_SCOPES = GRAPH_BASE_SCOPES


def _scopes_for_graph(include_contacts: bool) -> list[str]:
    return GRAPH_BASE_SCOPES + (GRAPH_CONTACT_SCOPES if include_contacts else [])


GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# calendarView/delta requires an explicit window. We default to ±2 years; events
# outside the window simply aren't synced (Outlook desktop uses a similar bound).
# We use calendarView/delta rather than events/delta because the latter
# returns a stripped-down event shape (only id/start/end/type/etag) on delta
# pages and explicitly rejects $select with change tracking — there's no way
# to ask it for subject/body. calendarView/delta returns full event JSON and
# we hydrate per-occurrence rows whose subject lives on the seriesMaster
# (see `_hydrate_occurrences_from_master` below).
_DELTA_WINDOW_PAST = timedelta(days=365)
_DELTA_WINDOW_FUTURE = timedelta(days=730)

# Map Microsoft Graph `responseStatus.response` → our normalized vocabulary.
# `organizer` / `none` / unknown → None (treated as "not invited").
_GRAPH_RESPONSE_MAP: dict[str, str] = {
    "accepted": "ACCEPTED",
    "tentativelyaccepted": "TENTATIVE",
    "declined": "DECLINED",
    "notresponded": "NEEDS-ACTION",
}

# Graph's recurrence pattern weekday name → iCal RRULE BYDAY code.
_GRAPH_WEEKDAY_TO_RRULE: dict[str, str] = {
    "sunday": "SU",
    "monday": "MO",
    "tuesday": "TU",
    "wednesday": "WE",
    "thursday": "TH",
    "friday": "FR",
    "saturday": "SA",
}

# Graph's `pattern.index` (for relative monthly/yearly recurrences) → BYDAY ordinal.
_GRAPH_INDEX_TO_RRULE: dict[str, str] = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
    "last": "-1",
}

# Microsoft Graph returns Windows timezone names; map them to IANA names for zoneinfo.
_WINDOWS_TZ_TO_IANA: dict[str, str] = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Greenland Standard Time": "America/Godthab",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Asia/Nicosia",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "India Standard Time": "Asia/Calcutta",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Rangoon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}


class GraphCursor(SyncCursor):
    _TYPE = "graph"

    def __init__(self, delta_link: str | None = None) -> None:
        self.delta_link = delta_link

    def to_json(self) -> dict[str, object]:
        return {"_type": self._TYPE, "delta_link": self.delta_link}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> GraphCursor:
        if data.get("_type") != cls._TYPE:
            raise ValueError(f"not a graph cursor: {data!r}")
        return cls(delta_link=cast("str | None", data.get("delta_link")))


def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _classify_one(exc: Exception) -> Exception:
    status = _status_of(exc)
    if status == 401 or status == 403:
        return AuthExpired(str(exc))
    if status == 410:
        return CursorExpired()
    if status == 412:
        return ConflictError(str(exc))
    if status is not None and (status == 429 or status >= 500):
        return TransientError(str(exc))
    return PermanentError(str(exc))


def _classify_errors(f):
    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except (
                AuthExpired,
                CursorExpired,
                ConflictError,
                TransientError,
                PermanentError,
            ):
                raise
            except Exception as exc:
                raise _classify_one(exc) from exc

        return wrapper

    @functools.wraps(f)
    async def wrapper_coro(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except (
            AuthExpired,
            CursorExpired,
            ConflictError,
            TransientError,
            PermanentError,
        ):
            raise
        except Exception as exc:
            raise _classify_one(exc) from exc

    return wrapper_coro


# Graph emits 7-digit fractional seconds (e.g. "...09:00:00.0000000") which
# datetime.fromisoformat rejects pre-3.11-style; strip extra digits so any
# Python version parses cleanly.
_FRACTIONAL_TRIM_RE = re.compile(r"\.(\d{6})\d+")


def _parse_graph_dt(raw: str | None, tz_hint: str | None) -> datetime | None:
    """Parse a Graph dateTime string and attach tzinfo from tz_hint.

    Graph always returns naive datetimes; the timezone arrives separately on
    the parent {dateTime, timeZone} object. For all-day events the timeZone is
    typically "UTC" and the resulting datetime represents midnight on the
    given local date — matching EventStore._ensure_aware_dt's convention.
    """
    if not raw:
        return None
    cleaned = _FRACTIONAL_TRIM_RE.sub(r".\1", raw)
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt
    if tz_hint and tz_hint != "UTC":
        iana = _WINDOWS_TZ_TO_IANA.get(tz_hint, tz_hint)
        try:
            return dt.replace(tzinfo=zoneinfo.ZoneInfo(iana))
        except Exception:
            pass
    return dt.replace(tzinfo=timezone.utc)


def _safe(fn, *, field: str, default=None):
    try:
        return fn()
    except Exception:
        log.exception("error extracting Graph field %s", field)
        return default


def _graph_recurrence_to_rrule(rec: dict[str, object] | None) -> str | None:
    """Convert a Graph `recurrence` object into an iCal RRULE string.

    Returns None if the structure is missing the required `pattern`/`range`
    sub-objects or the pattern type is unrecognized.
    """
    if not rec:
        return None
    pattern = cast("dict[str, object]", rec.get("pattern") or {})
    rng = cast("dict[str, object]", rec.get("range") or {})
    if not pattern or not rng:
        return None

    ptype = str(pattern.get("type") or "").lower()
    parts: list[str] = []

    if ptype == "daily":
        parts.append("FREQ=DAILY")
    elif ptype == "weekly":
        days_of_week = cast("list[str]", pattern.get("daysOfWeek") or [])
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in days_of_week
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        parts.append("FREQ=WEEKLY")
        if days:
            parts.append("BYDAY=" + ",".join(days))
    elif ptype == "absolutemonthly":
        parts.append("FREQ=MONTHLY")
        dom = cast("int", pattern.get("dayOfMonth"))
        if dom:
            parts.append(f"BYMONTHDAY={dom}")
    elif ptype == "relativemonthly":
        parts.append("FREQ=MONTHLY")
        index = _GRAPH_INDEX_TO_RRULE.get(str(pattern.get("index") or "").lower())
        days_of_week = cast("list[str]", pattern.get("daysOfWeek") or [])
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in days_of_week
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        if index and days:
            parts.append("BYDAY=" + ",".join(f"{index}{d}" for d in days))
    elif ptype == "absoluteyearly":
        parts.append("FREQ=YEARLY")
        month = cast("int", pattern.get("month"))
        dom = cast("int", pattern.get("dayOfMonth"))
        if month:
            parts.append(f"BYMONTH={month}")
        if dom:
            parts.append(f"BYMONTHDAY={dom}")
    elif ptype == "relativeyearly":
        parts.append("FREQ=YEARLY")
        month = cast("int", pattern.get("month"))
        index = _GRAPH_INDEX_TO_RRULE.get(str(pattern.get("index") or "").lower())
        days_of_week = cast("list[str]", pattern.get("daysOfWeek") or [])
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in days_of_week
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        if month:
            parts.append(f"BYMONTH={month}")
        if index and days:
            parts.append("BYDAY=" + ",".join(f"{index}{d}" for d in days))
    else:
        return None

    interval = cast("int | None", pattern.get("interval"))
    try:
        if interval is not None and int(interval) > 1:
            parts.append(f"INTERVAL={int(interval)}")
    except (TypeError, ValueError):
        pass

    rtype = str(rng.get("type") or "").lower()
    if rtype == "numbered":
        n = cast("int", rng.get("numberOfOccurrences"))
        if n:
            parts.append(f"COUNT={int(n)}")
    elif rtype == "enddate":
        # Graph emits `range.recurrenceTimeZone` (Windows-style names like
        # "Pacific Standard Time") which we don't currently convert; using
        # end-of-day UTC keeps the final day inclusive on most servers.
        end = rng.get("endDate")
        if end:
            try:
                d = datetime.strptime(str(end), "%Y-%m-%d")
                parts.append(f"UNTIL={d.strftime('%Y%m%d')}T235959Z")
            except ValueError:
                pass
    # "noend" → no tail
    return ";".join(parts)


def _graph_event_to_change(
    ev_json: dict[str, object], calendar_id: str
) -> EventChange | None:
    # Delta responses mark deletions with an "@removed" key on the otherwise-stub event.
    if "@removed" in ev_json:
        uid = cast("str", ev_json.get("id") or ev_json.get("iCalUId") or "")
        return EventChange(kind="delete", uid=uid)

    ev_type = str(ev_json.get("type") or "").lower()

    # Pre-expanded occurrences: the seriesMaster's rrule drives instance
    # generation via the expander — no need to store individual occurrence rows.
    if ev_type == "occurrence":
        return None

    uid = cast("str", ev_json.get("id") or ev_json.get("iCalUId") or "")
    body = cast("dict[str, object]", ev_json.get("body") or {})
    location = cast("dict[str, object]", ev_json.get("location") or {})

    start_obj = cast("dict[str, object]", ev_json.get("start") or {})
    end_obj = cast("dict[str, object]", ev_json.get("end") or {})
    raw_tz = str(start_obj.get("timeZone") or "UTC")
    # When the Prefer header is honoured, Graph returns the user's zone and
    # raw_tz won't be "UTC". If it still is (Prefer ignored or timed event with
    # no tz), use originalStartTimeZone when the server supplies one.
    if raw_tz == "UTC":
        original = cast("str", ev_json.get("originalStartTimeZone") or "")
        if original and original != "tzone://Microsoft/Custom":
            raw_tz = _WINDOWS_TZ_TO_IANA.get(original, original)
    tz: str = raw_tz
    dtstart = _safe(
        lambda: _parse_graph_dt(cast("str | None", start_obj.get("dateTime")), tz),
        field="start.dateTime",
    )
    dtend = _safe(
        lambda: _parse_graph_dt(
            cast("str | None", end_obj.get("dateTime")),
            cast("str | None", end_obj.get("timeZone")) or tz,
        ),
        field="end.dateTime",
    )

    all_day = bool(ev_json.get("isAllDay"))
    # Graph ignores Prefer: outlook.timezone for all-day events and always
    # returns midnight UTC. Strip the tz and re-anchor in the local zone so
    # that .date() in display code returns the right calendar day for users
    # west of UTC.
    if all_day and dtstart is not None:
        local_zone = local_zoneinfo()
        naive_start = (
            dtstart if dtstart.tzinfo is None else dtstart.replace(tzinfo=None)
        )
        dtstart = naive_start.replace(tzinfo=local_zone)
        if dtend is not None:
            naive_end = dtend if dtend.tzinfo is None else dtend.replace(tzinfo=None)
            dtend = naive_end.replace(tzinfo=local_zone)
        tz = local_iana_tz()
    status = "CANCELLED" if ev_json.get("isCancelled") else "CONFIRMED"
    show_as = str(ev_json.get("showAs") or "").lower()
    transparency = (
        "TRANSPARENT" if show_as in {"free", "workingelsewhere"} else "OPAQUE"
    )

    categories_raw = cast("list[object]", ev_json.get("categories") or [])
    categories = tuple(str(c) for c in categories_raw if c)

    attendees_raw = cast("list[object]", ev_json.get("attendees") or [])
    attendees: list[Attendee] = []
    # Graph exposes the user's own response at the top-level responseStatus block.
    response_obj = cast("dict[str, object]", ev_json.get("responseStatus") or {})
    self_response = _GRAPH_RESPONSE_MAP.get(
        str(response_obj.get("response") or "").lower()
    )
    organizer_obj = cast("dict[str, object]", ev_json.get("organizer") or {})
    organizer_email_obj = cast(
        "dict[str, object]", organizer_obj.get("emailAddress") or {}
    )
    organizer: Organizer | None = None
    if organizer_email_obj.get("address"):
        organizer = Organizer(
            email=str(organizer_email_obj["address"]),
            display_name=(
                str(organizer_email_obj["name"])
                if organizer_email_obj.get("name")
                else None
            ),
            is_self=bool(organizer_obj.get("self")),
        )
    for a in attendees_raw:
        if not isinstance(a, dict):
            continue
        ea = cast("dict[str, object]", a.get("emailAddress") or {})
        email = str(ea.get("address") or "")
        if not email:
            continue
        is_self = bool(a.get("self"))
        status_obj = cast("dict[str, object]", a.get("status") or {})
        response_raw = str(status_obj.get("response") or "").lower()
        resp = _GRAPH_RESPONSE_MAP.get(response_raw, "NEEDS-ACTION")
        is_organizer = response_raw == "organizer"
        attendees.append(
            Attendee(
                email=email,
                display_name=str(ea.get("name") or "") or None,
                response=resp,
                is_organizer=is_organizer,
                is_self=is_self,
            )
        )

    last_modified = _safe(
        lambda: _parse_graph_dt(
            cast("str | None", ev_json.get("lastModifiedDateTime")), "UTC"
        ),
        field="lastModifiedDateTime",
    )

    # Type-specific: seriesMaster carries the RRULE; exception carries a
    # recurrence_id and links back to its master.
    rrule: str | None = None
    recurrence_id: "datetime | None" = None
    if ev_type == "seriesmaster":
        rrule = _graph_recurrence_to_rrule(
            cast("dict[str, object] | None", ev_json.get("recurrence"))
        )
    elif ev_type == "exception":
        # Override instance: store under the master's uid so the expander can
        # find it as a sibling when rebuilding master instances.
        master_id = cast("str", ev_json.get("seriesMasterId") or "")
        if master_id:
            uid = master_id
        orig_start_obj = cast("dict[str, object]", ev_json.get("originalStart") or {})
        orig_tz = str(orig_start_obj.get("timeZone") or "UTC")
        recurrence_id = _safe(
            lambda: _parse_graph_dt(
                cast("str | None", orig_start_obj.get("dateTime")), orig_tz
            ),
            field="originalStart",
        )
        if recurrence_id is None:
            # Graph omits originalStart for attendee-view exceptions (the caller
            # is not the organizer). Without it we can't anchor the override to
            # the right occurrence slot; if we store it with recurrence_id=""
            # it collides with and overwrites the seriesMaster row, destroying
            # the rrule. Skip — the master's expansion covers all occurrences.
            return None

    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=cast("str | None", ev_json.get("id")),
        dtstart=dtstart,
        dtend=dtend,
        tz=tz,
        all_day=all_day,
        summary=cast("str", ev_json.get("subject", "") or ""),
        description=cast("str", body.get("content", "") or ""),
        location=cast("str", location.get("displayName", "") or ""),
        url=cast("str | None", ev_json.get("webLink")),
        rrule=rrule,
        recurrence_id=recurrence_id,
        attendees=tuple(attendees),
        organizer=organizer,
        categories=categories,
        status=status,
        self_response=self_response,
        transparency=transparency,
        last_modified=last_modified,
        etag=cast("str | None", ev_json.get("@odata.etag")),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


_IANA_TO_WINDOWS_TZ: dict[str, str] = {v: k for k, v in _WINDOWS_TZ_TO_IANA.items()}


def _rrule_to_graph_recurrence(
    rrule: str,
    dtstart: "datetime | None",
    dtend: "datetime | None",
) -> dict[str, object] | None:
    """Convert an iCal RRULE string back to a Graph recurrence object."""
    if not rrule:
        return None

    props: dict[str, str] = {}
    for part in rrule.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            props[k.upper()] = v

    freq = props.get("FREQ", "").upper()
    interval = int(props.get("INTERVAL", "1"))

    pattern: dict[str, object] = {"interval": interval}
    _rrule_to_graph_weekday = {v: k for k, v in _GRAPH_WEEKDAY_TO_RRULE.items()}
    _rrule_to_graph_index = {v: k for k, v in _GRAPH_INDEX_TO_RRULE.items()}

    if freq == "DAILY":
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        byday = props.get("BYDAY", "")
        days = [
            _rrule_to_graph_weekday.get(d.strip(), d.strip())
            for d in byday.split(",")
            if d.strip()
        ]
        if not days and dtstart:
            day_name = dtstart.strftime("%A").lower()
            days = [day_name]
        pattern["daysOfWeek"] = days
        if dtstart:
            pattern["firstDayOfWeek"] = "sunday"
    elif freq == "MONTHLY":
        byday = props.get("BYDAY", "")
        if byday and (byday[0].isdigit() or byday[0] == "-"):
            pattern["type"] = "relativeMonthly"
            # e.g. "2MO" → index="second", daysOfWeek=["monday"]
            ordinal = "".join(c for c in byday if c.isdigit() or c == "-")
            day_code = byday.lstrip("-0123456789")
            pattern["index"] = _rrule_to_graph_index.get(ordinal, "first")
            pattern["daysOfWeek"] = [_rrule_to_graph_weekday.get(day_code, day_code)]
        else:
            pattern["type"] = "absoluteMonthly"
            dom = props.get("BYMONTHDAY")
            if dom:
                pattern["dayOfMonth"] = int(dom)
            elif dtstart:
                pattern["dayOfMonth"] = dtstart.day
    elif freq == "YEARLY":
        byday = props.get("BYDAY", "")
        if byday:
            pattern["type"] = "relativeYearly"
            ordinal = "".join(c for c in byday if c.isdigit() or c == "-")
            day_code = byday.lstrip("-0123456789")
            pattern["index"] = _rrule_to_graph_index.get(ordinal, "first")
            pattern["daysOfWeek"] = [_rrule_to_graph_weekday.get(day_code, day_code)]
        else:
            pattern["type"] = "absoluteYearly"
        month = props.get("BYMONTH")
        if month:
            pattern["month"] = int(month)
        elif dtstart:
            pattern["month"] = dtstart.month
        dom = props.get("BYMONTHDAY")
        if dom:
            pattern["dayOfMonth"] = int(dom)
        elif dtstart:
            pattern["dayOfMonth"] = dtstart.day
    else:
        return None

    rng: dict[str, object] = {}
    if "COUNT" in props:
        rng["type"] = "numbered"
        rng["numberOfOccurrences"] = int(props["COUNT"])
    elif "UNTIL" in props:
        rng["type"] = "endDate"
        until_raw = props["UNTIL"]
        try:
            from datetime import datetime as _dt

            if "T" in until_raw:
                until_dt = _dt.strptime(until_raw[:15], "%Y%m%dT%H%M%S")
            else:
                until_dt = _dt.strptime(until_raw[:8], "%Y%m%d")
            rng["endDate"] = until_dt.strftime("%Y-%m-%d")
        except ValueError:
            rng["type"] = "noEnd"
    else:
        rng["type"] = "noEnd"

    if dtstart:
        rng["startDate"] = dtstart.strftime("%Y-%m-%d")
        iana = local_iana_tz()
        rng["recurrenceTimeZone"] = _IANA_TO_WINDOWS_TZ.get(iana, iana)

    return {"pattern": pattern, "range": rng}


def _event_to_graph_json(event: Event) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": event.summary,
        "body": {"contentType": "text", "content": event.description},
    }
    if event.location:
        body["location"] = {"displayName": event.location}
    if event.dtstart is not None:
        body["start"] = _datetime_to_graph(event.dtstart, event.tz, event.all_day)
    if event.dtend is not None:
        body["end"] = _datetime_to_graph(event.dtend, event.tz, event.all_day)
    if event.all_day:
        body["isAllDay"] = True
    if event.rrule:
        rec = _rrule_to_graph_recurrence(event.rrule, event.dtstart, event.dtend)
        if rec:
            body["recurrence"] = rec
    if event.transparency:
        body["showAs"] = "free" if event.transparency == "TRANSPARENT" else "busy"
    if event.categories:
        body["categories"] = list(event.categories)
    if event.attendees:
        attendees_list = [
            {"emailAddress": {"address": att.email, "name": att.display_name or ""}}
            for att in event.attendees
        ]
        if attendees_list:
            body["attendees"] = attendees_list
    return body


def _datetime_to_graph(dt: datetime, tz: str, all_day: bool) -> dict[str, str]:
    if all_day:
        return {"dateTime": dt.strftime("%Y-%m-%d"), "timeZone": tz or "UTC"}
    if dt.tzinfo is None:
        return {"dateTime": dt.isoformat(), "timeZone": tz or "UTC"}
    return {"dateTime": dt.isoformat(), "timeZone": tz or "UTC"}


def _delta_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - _DELTA_WINDOW_PAST).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + _DELTA_WINDOW_FUTURE).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


# Subset of seriesMaster fields we copy onto occurrences/exceptions whose
# own copy is blank. Graph keeps the source-of-truth subject/body/location on
# the master and only fills these on the children when they've been edited
# individually — so for ~all unmodified occurrences these come back empty.
_MASTER_HYDRATED_FIELDS: tuple[str, ...] = (
    "subject",
    "body",
    "location",
    "categories",
    "attendees",
    "showAs",
    "webLink",
)


def _new_msal_app(cache_json: str | None):
    import msal  # type: ignore[reportMissingTypeStubs]

    cache = msal.SerializableTokenCache()
    if cache_json:
        cache.deserialize(cache_json)
    app = msal.PublicClientApplication(
        GRAPH_CLIENT_ID,
        authority=GRAPH_AUTHORITY,
        token_cache=cache,
    )
    return app, cache


def _parse_pasted_redirect(text: str) -> tuple[str, str | None, str | None]:
    """Parse a pasted redirect URL or bare code.

    Returns (code, state, error). When text looks like a URL its query params
    are parsed; otherwise the whole string is treated as a bare code (state
    validation is then skipped by the caller).
    """
    import urllib.parse

    text = text.strip()
    if "?" in text or "nativeclient" in text:
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
            err = (qs.get("error") or [None])[0]
            err_desc = (qs.get("error_description") or [None])[0]
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            return (code or ""), state, err_desc or err
        except Exception:
            pass
    return text, None, None


def begin_graph_auth(
    include_contacts: bool = False,
) -> tuple[Any, Any, str, str]:
    """Build the auth URL for an interactive auth-code sign-in.

    Returns (app, cache, auth_url, state). The caller opens auth_url in a
    browser; when the user pastes back the redirect URL, pass everything to
    complete_graph_auth.
    """
    import secrets as _secrets

    app, cache = _new_msal_app(None)
    state = _secrets.token_urlsafe(16)
    auth_url = app.get_authorization_request_url(
        scopes=_scopes_for_graph(include_contacts),
        redirect_uri=GRAPH_REDIRECT_URI,
        state=state,
        prompt="select_account",
    )
    return app, cache, auth_url, state


def complete_graph_auth(
    app: Any,
    cache: Any,
    include_contacts: bool,
    pasted: str,
    expected_state: str,
) -> str:
    """Exchange the pasted redirect URL for a token; return serialized cache JSON."""
    code, returned_state, err = _parse_pasted_redirect(pasted)
    if err:
        raise RuntimeError(f"Microsoft returned an error: {err}")
    if not code:
        raise RuntimeError("No authorization code found in the pasted URL.")
    if returned_state is not None and returned_state != expected_state:
        raise RuntimeError("OAuth state mismatch — please try again.")
    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=_scopes_for_graph(include_contacts),
        redirect_uri=GRAPH_REDIRECT_URI,
    )
    if "error" in result:
        raise RuntimeError(result.get("error_description") or result["error"])
    return cache.serialize()


def _is_self_organizer(ev_json: dict, account_emails: frozenset[str]) -> bool:
    """True if the signed-in user organizes this event.

    Graph's `organizer.self` flag is unreliable when the login UPN differs
    from the mailbox primary SMTP (common with UPN/SMTP alias mismatches,
    delegate access, etc.). Fall back to comparing organizer.emailAddress.address
    against the user's known mailbox addresses.
    """
    org = cast("dict", ev_json.get("organizer") or {})
    if org.get("self"):
        return True
    addr = (
        str(cast("dict", org.get("emailAddress") or {}).get("address") or "")
        .strip()
        .lower()
    )
    return bool(addr and addr in account_emails)


class GraphBackend:
    def __init__(
        self,
        account_id: str,
        token_cache_json: str | None = None,
        on_token_refreshed: Callable[[str], None] | None = None,
        include_contacts: bool = False,
    ) -> None:
        self.account_id = account_id
        self._cache_json = token_cache_json
        self._on_token_refreshed = on_token_refreshed
        self._include_contacts = include_contacts
        self._http = None  # httpx.AsyncClient, created lazily
        self._account_emails: frozenset[str] | None = None  # cached lazily

    def _acquire_token(self) -> str:
        app, cache = _new_msal_app(self._cache_json)
        accounts = app.get_accounts()
        if not accounts:
            raise AuthExpired("no cached account; re-authenticate required")
        result = app.acquire_token_silent(
            _scopes_for_graph(self._include_contacts), account=accounts[0]
        )
        if not result or "access_token" not in result:
            raise AuthExpired("silent token acquisition failed")
        if cache.has_state_changed:
            self._cache_json = cache.serialize()
            if self._on_token_refreshed is not None:
                try:
                    self._on_token_refreshed(self._cache_json)
                except Exception:
                    log.exception("on_token_refreshed callback raised")
        return str(result["access_token"])

    async def _get_account_emails(self) -> frozenset[str]:
        """Fetch and cache the signed-in user's mailbox addresses.

        Graph's `organizer.self` flag is unreliable when the UPN differs from
        the primary SMTP address. Fetching /me gives us all known addresses so
        we can detect organizer-self by email comparison instead.
        """
        if self._account_emails is not None:
            return self._account_emails
        try:
            resp = await self._request(
                "GET", "/me?$select=mail,userPrincipalName,proxyAddresses"
            )
            data = resp.json()
            addrs: set[str] = set()
            for k in ("mail", "userPrincipalName"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    addrs.add(v.strip().lower())
            for pa in data.get("proxyAddresses") or []:
                if not isinstance(pa, str):
                    continue
                _, _, rest = pa.partition(":")
                if rest:
                    addrs.add(rest.strip().lower())
            self._account_emails = frozenset(addrs)
        except Exception:
            log.exception(
                "graph: failed to fetch /me account addresses; "
                "organizer-self detection will rely on organizer.self flag only"
            )
            self._account_emails = frozenset()
        return self._account_emails

    def _get_http(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ):
        import httpx

        token = await asyncio.to_thread(self._acquire_token)
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if method.upper() == "GET":
            hdrs["Prefer"] = f'outlook.timezone="{local_iana_tz()}"'
        if headers:
            hdrs.update(headers)
        full = url if url.startswith("http") else f"{GRAPH_BASE}{url}"
        client = self._get_http()
        try:
            resp = await client.request(method, full, json=json_body, headers=hdrs)
        except httpx.HTTPError as exc:
            raise TransientError(str(exc)) from exc
        if resp.status_code >= 400:
            # Graph's response body holds the real reason (e.g. "occurrence of a
            # series can't be deleted directly"). httpx.raise_for_status drops it,
            # so attach it ourselves before re-raising.
            body_snippet = ""
            with contextlib.suppress(Exception):
                body_snippet = resp.text[:500]
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if body_snippet:
                    exc.args = (
                        f"{exc.args[0] if exc.args else ''} body={body_snippet}",
                    )
                raise
        return resp

    # Microsoft Graph color enum → hex map.
    # Source: https://learn.microsoft.com/en-us/graph/api/resources/calendar
    _GRAPH_COLOR_MAP: dict[str, str] = {
        "lightBlue": "#5e9fff",
        "lightGreen": "#5cc97a",
        "lightOrange": "#f59e0b",
        "lightGray": "#9ca3af",
        "lightYellow": "#eab308",
        "lightTeal": "#14b8a6",
        "lightPink": "#ec4899",
        "lightBrown": "#a16207",
        "lightRed": "#ef4444",
        "maxColor": "#6366f1",
    }

    @_classify_errors
    async def list_calendars(self) -> list[dict[str, object]]:
        resp = await self._request(
            "GET", "/me/calendars?$select=id,name,color,hexColor"
        )
        data = resp.json()
        out = []
        for cal in data.get("value", []):
            # Prefer hexColor (newer field, exact hex) over the color enum.
            colour = cal.get("hexColor") or self._GRAPH_COLOR_MAP.get(
                cal.get("color", "")
            )
            if colour and not colour.startswith("#"):
                colour = "#" + colour
            out.append(
                {
                    "id": cal.get("id", ""),
                    "display_name": cal.get("name") or cal.get("id") or "",
                    "provider_id": cal.get("id", ""),
                    "color": (colour or "").lower() or None,
                }
            )
        return out

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        start, end = _delta_window()
        url = (
            f"/me/calendars/{calendar_id}/calendarView/delta"
            f"?startDateTime={start}&endDateTime={end}"
        )
        async for batch, cursor in self._drain_delta(url, calendar_id):
            yield batch, cursor

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        if not isinstance(cursor, GraphCursor) or not cursor.delta_link:
            raise CursorExpired(calendar_id)
        delta_link = cursor.delta_link
        changes: list[EventChange] = []
        new_cursor = GraphCursor()
        async for batch, c in self._drain_delta(delta_link, calendar_id):
            changes.extend(batch)
            new_cursor = c  # last yielded cursor carries the final delta link
        return changes, new_cursor

    async def _drain_delta(
        self, url: str, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        next_url: str | None = url
        masters_cache: dict[
            str, dict[str, object]
        ] = {}  # shared across pages within one sync
        while next_url:
            resp = await self._request("GET", next_url)
            data = resp.json()
            events = data.get("value", [])
            await self._hydrate_and_synthesize_masters(events, masters_cache)
            await self._refresh_organizer_attendee_responses(events)
            batch = [
                c
                for c in (_graph_event_to_change(ev, calendar_id) for ev in events)
                if c is not None
            ]
            delta = data.get("@odata.deltaLink")
            link = data.get("@odata.nextLink")
            cursor = GraphCursor(delta_link=delta) if delta else GraphCursor()
            yield batch, cursor
            next_url = link

    async def _graph_batch_get(self, ids: list[str]) -> dict[str, dict[str, object]]:
        """Fetch Graph event objects via the $batch endpoint (≤20 per POST)."""
        result: dict[str, dict[str, object]] = {}
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            body = {
                "requests": [
                    {"id": mid, "method": "GET", "url": f"/me/events/{mid}"}
                    for mid in chunk
                ]
            }
            try:
                resp = await self._request(
                    "POST", "/$batch", json_body=cast("dict[str, object]", body)
                )
                for item in resp.json().get("responses", []):
                    if item.get("status") == 200:
                        result[item["id"]] = item["body"]
            except Exception:
                log.exception("$batch fetch failed for %d masters", len(chunk))
        return result

    async def _graph_batch_get_urls(
        self, urls: dict[str, str]
    ) -> dict[str, dict[str, object]]:
        """Fetch arbitrary Graph URLs via $batch (≤20 per POST).

        `urls` maps a caller-defined key to a relative Graph URL.
        Returns a dict of the same keys → response body (only 200 responses).
        """
        result: dict[str, dict[str, object]] = {}
        items = list(urls.items())
        for i in range(0, len(items), 20):
            chunk = items[i : i + 20]
            body = {
                "requests": [
                    {"id": key, "method": "GET", "url": url} for key, url in chunk
                ]
            }
            try:
                resp = await self._request(
                    "POST", "/$batch", json_body=cast("dict[str, object]", body)
                )
                for item in resp.json().get("responses", []):
                    if item.get("status") == 200:
                        result[item["id"]] = item["body"]
            except Exception:
                log.exception("$batch fetch failed for %d urls", len(chunk))
        return result

    async def _refresh_organizer_attendee_responses(
        self, events: list[dict[str, object]]
    ) -> None:
        """Overwrite stale 'none' attendee responses on organizer-owned seriesMasters.

        Graph's calendarView/delta returns attendees[].status.response='none' for
        recurring series that the signed-in user organizes — even after invitees
        have responded. Real responses are only available on expanded occurrence
        rows via /instances. We fetch one upcoming instance per affected master
        and overwrite the master's attendees array in place.
        """
        account_emails = await self._get_account_emails()

        now = datetime.now(timezone.utc)
        window_start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end = (now + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")

        n_masters = 0
        n_self_org = 0
        n_stale = 0
        candidates: list[tuple[int, str]] = []
        for i, ev in enumerate(events):
            if str(ev.get("type") or "").lower() != "seriesmaster":
                continue
            n_masters += 1
            if not _is_self_organizer(ev, account_emails):
                continue
            n_self_org += 1
            attendees = ev.get("attendees") or []
            if not attendees:
                continue
            if not any(
                str(((a.get("status") or {}).get("response") or "")).lower()
                in {"", "none", "notresponded"}
                for a in attendees
                if isinstance(a, dict)
            ):
                continue
            n_stale += 1
            candidates.append((i, str(ev.get("id"))))

        log.info(
            "graph attendee refresh: scanned=%d masters=%d self_organized=%d "
            "stale_attendees=%d candidates=%d",
            len(events),
            n_masters,
            n_self_org,
            n_stale,
            len(candidates),
        )
        if not candidates:
            return

        urls = {
            master_id: (
                f"/me/events/{master_id}/instances"
                f"?startDateTime={window_start}"
                f"&endDateTime={window_end}"
                f"&$top=1&$select=attendees,start"
            )
            for _, master_id in candidates
        }
        responses = await self._graph_batch_get_urls(urls)

        masters_updated = 0
        attendees_replaced = 0
        for idx, master_id in candidates:
            body = responses.get(master_id)
            if not body:
                log.debug(
                    "graph attendee refresh: no response for master %s", master_id[-20:]
                )
                continue
            instances = cast("list[dict[str, object]]", body.get("value") or [])
            if not instances:
                log.debug(
                    "graph attendee refresh: /instances returned empty for master %s"
                    " (window %s – %s)",
                    master_id[-20:],
                    window_start[:10],
                    window_end[:10],
                )
                continue
            fresh_attendees = cast(
                "list[object]", instances[0].get("attendees") or []
            )
            if fresh_attendees:
                events[idx]["attendees"] = fresh_attendees
                masters_updated += 1
                attendees_replaced += len(fresh_attendees)
                instance_start = (instances[0].get("start") or {}).get("dateTime", "?")[
                    :10
                ]
                log.debug(
                    "graph attendee refresh: updated master %s with %d attendees"
                    " from instance %s",
                    master_id[-20:],
                    len(fresh_attendees),
                    instance_start,
                )

        log.info(
            "graph attendee refresh: candidates=%d instances_fetched=%d "
            "masters_updated=%d attendees_replaced=%d",
            len(candidates),
            len(responses),
            masters_updated,
            attendees_replaced,
        )

    async def _hydrate_and_synthesize_masters(
        self,
        events: list[dict[str, object]],
        masters_cache: dict[str, dict[str, object]],
    ) -> None:
        """Fetch missing seriesMaster rows and inject them into the events list.

        calendarView/delta pre-expands recurring series into occurrence rows
        and only returns the seriesMaster directly when its DTSTART falls
        inside the calendar view window. For long-running series (started
        more than a year ago) the seriesMaster is absent; occurrences carry
        only a `seriesMasterId` back-reference. Without the master we have no
        rrule and the entire series becomes invisible.

        For every occurrence or exception whose seriesMasterId isn't already
        in the current page or in the cross-page `masters_cache`, we fetch
        the master via $batch and inject the JSON into `events` so the
        downstream _graph_event_to_change pass produces a seriesMaster
        EventChange with rrule populated. Masters already in the page (id
        present in existing events) are skipped. The cache prevents re-fetching
        the same master on subsequent pages.

        Subject/body/location are also hydrated onto exception rows as before.
        """
        # IDs of seriesMaster events already present in this page.
        masters_in_page: set[str] = {
            cast("str", ev.get("id") or "")
            for ev in events
            if str(ev.get("type") or "").lower() == "seriesmaster"
        }

        master_ids: set[str] = set()
        for ev in events:
            if "@removed" in ev:
                continue
            ev_type = str(ev.get("type") or "").lower()
            smi = cast("str", ev.get("seriesMasterId") or "")
            if not smi:
                continue
            # Already in the page or already cached — no fetch needed.
            if smi in masters_in_page or smi in masters_cache:
                continue
            # Collect from both occurrences (to synthesize master) and
            # exceptions (to hydrate subject + synthesize master).
            if ev_type in {"occurrence", "exception"}:
                master_ids.add(smi)

        if master_ids:
            fetched = await self._graph_batch_get(list(master_ids))
            masters_cache.update(fetched)
            # Inject fetched seriesMasters into the events list so
            # _graph_event_to_change produces EventChange rows with rrule.
            for _mid, master_json in fetched.items():
                if str(master_json.get("type") or "").lower() == "seriesmaster":
                    events.append(master_json)

        # Hydrate subject/body/location onto exception rows whose fields are blank.
        for ev in events:
            mid = cast("str", ev.get("seriesMasterId") or "")
            if not mid or mid not in masters_cache:
                continue
            master = masters_cache[mid]
            for field in _MASTER_HYDRATED_FIELDS:
                cur = ev.get(field)
                hydrate = False
                if not cur:
                    hydrate = True
                elif isinstance(cur, dict):
                    if not (cur.get("displayName") or cur.get("content")):
                        hydrate = True
                elif isinstance(cur, list) and not cur:
                    hydrate = True
                if hydrate and master.get(field) is not None:
                    ev[field] = master[field]

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        resp = await self._request(
            "POST",
            f"/me/calendars/{calendar_id}/events",
            json_body=_event_to_graph_json(event),
        )
        data = resp.json()
        return Event(
            uid=data.get("id") or event.uid,
            calendar_id=calendar_id,
            provider_event_id=data.get("id"),
            summary=event.summary,
            description=event.description,
            location=event.location,
            dtstart=event.dtstart,
            dtend=event.dtend,
            tz=event.tz,
            all_day=event.all_day,
            etag=data.get("@odata.etag"),
        )

    @_classify_errors
    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        if not event.provider_event_id:
            raise PermanentError("update_event requires provider_event_id")
        headers = {"If-Match": if_match} if if_match else None
        resp = await self._request(
            "PATCH",
            f"/me/events/{event.provider_event_id}",
            json_body=_event_to_graph_json(event),
            headers=headers,
        )
        data = resp.json()
        return Event(
            uid=event.uid,
            calendar_id=calendar_id,
            provider_event_id=event.provider_event_id,
            summary=event.summary,
            description=event.description,
            location=event.location,
            dtstart=event.dtstart,
            dtend=event.dtend,
            tz=event.tz,
            all_day=event.all_day,
            etag=data.get("@odata.etag", event.etag),
        )

    @_classify_errors
    async def delete_event(
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None:
        # Caller passes provider_event_id in uid (Graph deletes by event id).
        headers = {"If-Match": if_match} if if_match else None
        await self._request(
            "DELETE",
            f"/me/events/{uid}",
            headers=headers,
        )

    @_classify_errors
    async def update_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: "datetime",
        event: Event,
    ) -> None:
        """Update a single occurrence of a recurring series.

        Resolves the Graph occurrence id by listing instances around the
        recurrence_id datetime, then PATCHes that specific occurrence.
        """
        from datetime import timedelta

        win_start = (recurrence_id_dt - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        win_end = (recurrence_id_dt + timedelta(hours=25)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        resp = await self._request(
            "GET",
            f"/me/events/{master_provider_id}/instances"
            f"?startDateTime={win_start}&endDateTime={win_end}"
            f"&$select=id,start",
        )
        items = resp.json().get("value", [])
        instance_id: str | None = None
        rid_utc = recurrence_id_dt.astimezone(timezone.utc)
        for item in items:
            start_part = item.get("start") or {}
            start_raw = start_part.get("dateTime", "")
            item_tz_name = start_part.get("timeZone") or "UTC"
            try:
                import zoneinfo as _zi

                item_tz = _zi.ZoneInfo(item_tz_name)
                item_dt = datetime.fromisoformat(start_raw).replace(tzinfo=item_tz)
            except Exception:
                try:
                    item_dt = datetime.fromisoformat(start_raw.rstrip("Z")).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
            if abs((item_dt.astimezone(timezone.utc) - rid_utc).total_seconds()) < 300:
                instance_id = item.get("id")
                break
        if not instance_id:
            raise PermanentError(
                f"Could not find Graph occurrence for {recurrence_id_dt.isoformat()}"
            )
        await self._request(
            "PATCH",
            f"/me/events/{instance_id}",
            json_body=_event_to_graph_json(event),
        )

    @_classify_errors
    async def respond_to_event(
        self, calendar_id: str, event: Event, response: str
    ) -> Event | None:
        if not event.provider_event_id:
            return None
        _endpoints: dict[str, str] = {
            "ACCEPTED": "accept",
            "TENTATIVE": "tentativelyAccept",
            "DECLINED": "decline",
        }
        action = _endpoints.get(response.upper())
        if not action:
            return None
        # Graph RSVP endpoints return HTTP 202 with no body.
        await self._request(
            "POST",
            f"/me/events/{event.provider_event_id}/{action}",
            json_body={"sendResponse": True, "comment": ""},
        )
        return None  # etag will be refreshed by the next incremental sync

    @_classify_errors
    async def delete_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: "datetime",
    ) -> None:
        """Cancel a single occurrence of a recurring series."""
        from datetime import timedelta

        win_start = (recurrence_id_dt - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        win_end = (recurrence_id_dt + timedelta(hours=25)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        resp = await self._request(
            "GET",
            f"/me/events/{master_provider_id}/instances"
            f"?startDateTime={win_start}&endDateTime={win_end}"
            f"&$select=id,start",
        )
        items = resp.json().get("value", [])
        instance_id: str | None = None
        rid_utc = recurrence_id_dt.astimezone(timezone.utc)
        for item in items:
            start_part = item.get("start") or {}
            start_raw = start_part.get("dateTime", "")
            item_tz_name = start_part.get("timeZone") or "UTC"
            try:
                import zoneinfo as _zi

                item_tz = _zi.ZoneInfo(item_tz_name)
                item_dt = datetime.fromisoformat(start_raw).replace(tzinfo=item_tz)
            except Exception:
                try:
                    item_dt = datetime.fromisoformat(start_raw.rstrip("Z")).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
            if abs((item_dt.astimezone(timezone.utc) - rid_utc).total_seconds()) < 300:
                instance_id = item.get("id")
                break
        if not instance_id:
            raise PermanentError(
                f"Could not find Graph occurrence for {recurrence_id_dt.isoformat()}"
            )
        await self._request("DELETE", f"/me/events/{instance_id}")

    def supported_contact_sources(self) -> tuple[str, ...]:
        if self._include_contacts:
            return ("other", "personal", "directory")
        return ()

    @_classify_errors
    async def list_contacts(
        self, source: str, cursor: dict | None
    ) -> tuple[list[Contact], dict | None, bool]:
        contacts: list[Contact] = []
        if source == "other":
            # /me/people — relevance-ranked from mail+calendar interactions.
            skip = (cursor or {}).get("skip", 0)
            resp = await self._request(
                "GET",
                f"/me/people?$top=500&$skip={skip}&$select=id,displayName,scoredEmailAddresses,personType",
            )
            data = resp.json()
            people = data.get("value") or []
            for p in people:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("displayName") or "").strip() or None
                for ea in p.get("scoredEmailAddresses") or []:
                    addr = (
                        str(ea.get("address") or "").strip().lower()
                        if isinstance(ea, dict)
                        else ""
                    )
                    if addr and "@" in addr:
                        contacts.append(
                            Contact(
                                email=addr,
                                display_name=name,
                                source="other",
                                account_id=self.account_id,
                                source_id=str(p.get("id") or "") or None,
                            )
                        )
            next_link = data.get("@odata.nextLink")
            if next_link:
                return contacts, {"skip": skip + len(people)}, False
            return contacts, None, True

        if source == "personal":
            skip_token = (cursor or {}).get("skipToken", "")
            url = "/me/contacts?$top=100&$select=id,displayName,emailAddresses"
            if skip_token:
                url += f"&$skiptoken={skip_token}"
            resp = await self._request("GET", url)
            data = resp.json()
            for c in data.get("value") or []:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("displayName") or "").strip() or None
                for ea in c.get("emailAddresses") or []:
                    addr = (
                        str(ea.get("address") or "").strip().lower()
                        if isinstance(ea, dict)
                        else ""
                    )
                    if addr and "@" in addr:
                        contacts.append(
                            Contact(
                                email=addr,
                                display_name=name,
                                source="personal",
                                account_id=self.account_id,
                                source_id=str(c.get("id") or "") or None,
                            )
                        )
            next_link = data.get("@odata.nextLink")
            if next_link:
                import urllib.parse as _up

                parsed = _up.urlparse(next_link)
                qs = dict(_up.parse_qsl(parsed.query))
                return contacts, {"skipToken": qs.get("$skiptoken", "")}, False
            return contacts, None, True

        if source == "directory":
            skip_token = (cursor or {}).get("skipToken", "")
            url = "/users?$top=999&$select=id,displayName,mail,userPrincipalName"
            if skip_token:
                url += f"&$skiptoken={skip_token}"
            resp = await self._request("GET", url)
            data = resp.json()
            for u in data.get("value") or []:
                if not isinstance(u, dict):
                    continue
                name = str(u.get("displayName") or "").strip() or None
                for field in ("mail", "userPrincipalName"):
                    addr = str(u.get(field) or "").strip().lower()
                    if addr and "@" in addr:
                        contacts.append(
                            Contact(
                                email=addr,
                                display_name=name,
                                source="directory",
                                account_id=self.account_id,
                                source_id=str(u.get("id") or "") or None,
                            )
                        )
                        break
            next_link = data.get("@odata.nextLink")
            if next_link:
                import urllib.parse as _up

                parsed = _up.urlparse(next_link)
                qs = dict(_up.parse_qsl(parsed.query))
                return contacts, {"skipToken": qs.get("$skiptoken", "")}, False
            return contacts, None, True

        return [], None, True

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
