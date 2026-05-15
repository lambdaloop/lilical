from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    EventChange,
    PermanentError,
    SyncCursor,
    TransientError,
)
from lilical.models.event import Event
from lilical.utils.timezone import local_iana_tz, local_zoneinfo

log = logging.getLogger(__name__)

# Evolution's public Microsoft 365 client ID. We piggy-back on it because (a) it's
# a registered, multi-tenant public client, (b) many corporate tenants that block
# generic "unknown app" consent already allow Evolution, and (c) Thunderbird's
# client ID hits "needs admin approval" in stricter tenants where Evolution does
# not. Trade-off: the user-facing consent screen reads "Evolution / GNOME".
GRAPH_CLIENT_ID = "20460e5d-ce91-49af-a3a5-70b6be7486d1"
GRAPH_AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_SCOPES = ["Calendars.ReadWrite", "User.Read"]
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

    def to_json(self) -> dict:
        return {"_type": self._TYPE, "delta_link": self.delta_link}

    @classmethod
    def from_json(cls, data: dict) -> GraphCursor:
        if data.get("_type") != cls._TYPE:
            raise ValueError(f"not a graph cursor: {data!r}")
        return cls(delta_link=data.get("delta_link"))


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
    async def wrapper(*args, **kwargs):
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

    return wrapper


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


def _graph_recurrence_to_rrule(rec: dict | None) -> str | None:
    """Convert a Graph `recurrence` object into an iCal RRULE string.

    Returns None if the structure is missing the required `pattern`/`range`
    sub-objects or the pattern type is unrecognized.
    """
    if not rec:
        return None
    pattern = rec.get("pattern") or {}
    rng = rec.get("range") or {}
    if not pattern or not rng:
        return None

    ptype = str(pattern.get("type") or "").lower()
    parts: list[str] = []

    if ptype == "daily":
        parts.append("FREQ=DAILY")
    elif ptype == "weekly":
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in (pattern.get("daysOfWeek") or [])
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        parts.append("FREQ=WEEKLY")
        if days:
            parts.append("BYDAY=" + ",".join(days))
    elif ptype == "absolutemonthly":
        parts.append("FREQ=MONTHLY")
        dom = pattern.get("dayOfMonth")
        if dom:
            parts.append(f"BYMONTHDAY={int(dom)}")
    elif ptype == "relativemonthly":
        parts.append("FREQ=MONTHLY")
        index = _GRAPH_INDEX_TO_RRULE.get(str(pattern.get("index") or "").lower())
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in (pattern.get("daysOfWeek") or [])
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        if index and days:
            parts.append("BYDAY=" + ",".join(f"{index}{d}" for d in days))
    elif ptype == "absoluteyearly":
        parts.append("FREQ=YEARLY")
        month = pattern.get("month")
        dom = pattern.get("dayOfMonth")
        if month:
            parts.append(f"BYMONTH={int(month)}")
        if dom:
            parts.append(f"BYMONTHDAY={int(dom)}")
    elif ptype == "relativeyearly":
        parts.append("FREQ=YEARLY")
        month = pattern.get("month")
        index = _GRAPH_INDEX_TO_RRULE.get(str(pattern.get("index") or "").lower())
        days = [
            _GRAPH_WEEKDAY_TO_RRULE[d.lower()]
            for d in (pattern.get("daysOfWeek") or [])
            if d and d.lower() in _GRAPH_WEEKDAY_TO_RRULE
        ]
        if month:
            parts.append(f"BYMONTH={int(month)}")
        if index and days:
            parts.append("BYDAY=" + ",".join(f"{index}{d}" for d in days))
    else:
        return None

    interval = pattern.get("interval")
    try:
        if interval is not None and int(interval) > 1:
            parts.append(f"INTERVAL={int(interval)}")
    except (TypeError, ValueError):
        pass

    rtype = str(rng.get("type") or "").lower()
    if rtype == "numbered":
        n = rng.get("numberOfOccurrences")
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


def _graph_event_to_change(ev_json: dict, calendar_id: str) -> EventChange | None:
    # Delta responses mark deletions with an "@removed" key on the otherwise-stub event.
    # We key local rows on Graph's `id`, not `iCalUId` — calendarView/delta
    # pre-expands recurring events, so every occurrence shares an iCalUId but
    # has a distinct id. `id` is also what /me/events/{id} expects for delete.
    if "@removed" in ev_json:
        uid = ev_json.get("id") or ev_json.get("iCalUId") or ""
        return EventChange(kind="delete", uid=uid)

    ev_type = str(ev_json.get("type") or "").lower()
    if ev_type == "seriesmaster":
        # calendarView/delta returns this master's occurrences as individual
        # events, each with a unique id. Storing the master row too causes
        # _rebuild_instances_for to expand its RRULE into N instances on top
        # of the N per-occurrence instances already produced — duplicating
        # every recurring event.
        return None

    uid = ev_json.get("id") or ev_json.get("iCalUId") or ""
    body = ev_json.get("body") or {}
    location = ev_json.get("location") or {}

    start_obj = ev_json.get("start") or {}
    end_obj = ev_json.get("end") or {}
    raw_tz = str(start_obj.get("timeZone") or "UTC")
    # When the Prefer header is honoured, Graph returns the user's zone and
    # raw_tz won't be "UTC". If it still is (Prefer ignored or timed event with
    # no tz), use originalStartTimeZone when the server supplies one.
    if raw_tz == "UTC":
        original = ev_json.get("originalStartTimeZone") or ""
        if original and original != "tzone://Microsoft/Custom":
            raw_tz = _WINDOWS_TZ_TO_IANA.get(original, original)
    tz = raw_tz
    dtstart = _safe(
        lambda: _parse_graph_dt(start_obj.get("dateTime"), tz),
        field="start.dateTime",
    )
    dtend = _safe(
        lambda: _parse_graph_dt(end_obj.get("dateTime"), end_obj.get("timeZone") or tz),
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

    categories_raw = ev_json.get("categories") or []
    categories = tuple(str(c) for c in categories_raw if c)

    attendees_raw = ev_json.get("attendees") or []
    attendees: list[str] = []
    for a in attendees_raw:
        email = (
            (a.get("emailAddress") or {}).get("address")
            if isinstance(a, dict)
            else None
        )
        if email:
            attendees.append(str(email))

    # Microsoft Graph exposes the current user's response at the event level.
    response_obj = ev_json.get("responseStatus") or {}
    self_response = _GRAPH_RESPONSE_MAP.get(
        str(response_obj.get("response") or "").lower()
    )

    last_modified = _safe(
        lambda: _parse_graph_dt(ev_json.get("lastModifiedDateTime"), "UTC"),
        field="lastModifiedDateTime",
    )

    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=ev_json.get("id"),
        dtstart=dtstart,
        dtend=dtend,
        tz=tz,
        all_day=all_day,
        summary=ev_json.get("subject", "") or "",
        description=body.get("content", "") or "",
        location=location.get("displayName", "") or "",
        url=ev_json.get("webLink"),
        rrule=None,
        attendees=tuple(attendees),
        categories=categories,
        status=status,
        self_response=self_response,
        transparency=transparency,
        last_modified=last_modified,
        etag=ev_json.get("@odata.etag"),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


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
    import msal

    cache = msal.SerializableTokenCache()
    if cache_json:
        cache.deserialize(cache_json)
    app = msal.PublicClientApplication(
        GRAPH_CLIENT_ID,
        authority=GRAPH_AUTHORITY,
        token_cache=cache,
    )
    return app, cache


def initiate_graph_device_flow() -> tuple[Any, Any, dict]:
    """Start a device-code flow. Returns (msal_app, cache, flow_dict).

    The flow_dict has 'user_code', 'verification_uri', 'message', 'expires_in'.
    Pass the same app/cache/flow to complete_graph_device_flow to block until done.
    """
    app, cache = _new_msal_app(None)
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        err = flow.get("error_description") or flow.get("error") or "init failed"
        raise RuntimeError(str(err))
    return app, cache, flow


def complete_graph_device_flow(app: Any, cache: Any, flow: dict) -> str:
    """Block until the user signs in via the device code; return serialized cache."""
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "auth failed"
        raise RuntimeError(str(err))
    return cache.serialize()


class GraphBackend:
    def __init__(
        self,
        account_id: str,
        token_cache_json: str | None = None,
        on_token_refreshed: Callable[[str], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self._cache_json = token_cache_json
        self._on_token_refreshed = on_token_refreshed
        self._http = None  # httpx.AsyncClient, created lazily

    def _acquire_token(self) -> str:
        app, cache = _new_msal_app(self._cache_json)
        accounts = app.get_accounts()
        if not accounts:
            raise AuthExpired("no cached account; re-authenticate required")
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise AuthExpired("silent token acquisition failed")
        if cache.has_state_changed:
            self._cache_json = cache.serialize()
            if self._on_token_refreshed is not None:
                try:
                    self._on_token_refreshed(self._cache_json)
                except Exception:
                    log.exception("on_token_refreshed callback raised")
        return result["access_token"]

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
        json_body: dict | None = None,
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
            try:
                body_snippet = resp.text[:500]
            except Exception:
                pass
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
    async def list_calendars(self) -> list:
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
        masters_cache: dict[str, dict] = {}  # shared across pages within one sync
        while next_url:
            resp = await self._request("GET", next_url)
            data = resp.json()
            events = data.get("value", [])
            await self._hydrate_occurrences_from_master(events, masters_cache)
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

    async def _graph_batch_get(self, ids: list[str]) -> dict[str, dict]:
        """Fetch Graph event objects via the $batch endpoint (≤20 per POST)."""
        result: dict[str, dict] = {}
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            body = {
                "requests": [
                    {"id": mid, "method": "GET", "url": f"/me/events/{mid}"}
                    for mid in chunk
                ]
            }
            try:
                resp = await self._request("POST", "/$batch", json_body=body)
                for item in resp.json().get("responses", []):
                    if item.get("status") == 200:
                        result[item["id"]] = item["body"]
            except Exception:
                log.exception("$batch fetch failed for %d masters", len(chunk))
        return result

    async def _hydrate_occurrences_from_master(
        self, events: list[dict], masters_cache: dict[str, dict]
    ) -> None:
        """Fill in subject/body/location on calendarView occurrences that
        share their data with a seriesMaster.

        calendarView/delta pre-expands recurring events and only populates
        fields like `subject` on occurrences that have been individually
        edited — for an unmodified weekly meeting Graph returns blank
        subject/body/location and expects the caller to pull them from the
        master via `seriesMasterId`. Masters are fetched in a single $batch
        call and cached in `masters_cache` across pages so multi-page syncs
        never re-fetch the same master.
        """
        master_ids: set[str] = set()
        for ev in events:
            if "@removed" in ev:
                continue
            if not (ev.get("subject") or "").strip():
                smi = ev.get("seriesMasterId")
                if smi and smi not in masters_cache:
                    master_ids.add(smi)
        if master_ids:
            fetched = await self._graph_batch_get(list(master_ids))
            masters_cache.update(fetched)

        for ev in events:
            mid = ev.get("seriesMasterId")
            if not mid or mid not in masters_cache:
                continue
            master = masters_cache[mid]
            for field in _MASTER_HYDRATED_FIELDS:
                cur = ev.get(field)
                if (
                    not cur
                    or (
                        isinstance(cur, dict)
                        and not (cur.get("displayName") or cur.get("content"))
                    )
                    or (isinstance(cur, list) and not cur)
                ):
                    if master.get(field) is not None:
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
            uid=data.get("iCalUId") or event.uid,
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

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
