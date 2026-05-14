from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import zoneinfo
from dataclasses import dataclass
from datetime import date as _date_cls
from datetime import datetime, time, timedelta, timezone
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

import caldav
import icalendar
from caldav.lib.error import AuthorizationError, DAVError

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

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CalDavCursor(SyncCursor):
    sync_token: str | None = None
    ctag: str | None = None

    def to_json(self) -> dict:
        return {"sync_token": self.sync_token, "ctag": self.ctag}

    @classmethod
    def from_json(cls, data: dict) -> CalDavCursor:
        return cls(sync_token=data.get("sync_token"), ctag=data.get("ctag"))


def _dav_status(e: DAVError) -> int | None:
    """Parse HTTP status code out of a DAVError's url field (e.g. 'HTTP/1.1 507 ...')."""
    url = getattr(e, "url", None) or ""
    m = re.search(r"\b([1-5][0-9]{2})\b", url)
    return int(m.group(1)) if m else None


def _classify_errors(f):
    op = f.__name__

    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except AuthorizationError as e:
                raise AuthExpired(f"{op}: {e}") from e
            except DAVError as e:
                status = _dav_status(e)
                msg = f"{op}: {e}"
                if status in (401, 403):
                    raise AuthExpired(msg) from e
                if status == 410:
                    raise CursorExpired() from e
                if status == 412:
                    raise ConflictError(msg) from e
                if status is not None and status >= 500:
                    raise TransientError(msg) from e
                raise TransientError(msg) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", op)
                raise PermanentError(f"{op}: {e}") from e

        return wrapper
    else:

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except AuthorizationError as e:
                raise AuthExpired(f"{op}: {e}") from e
            except DAVError as e:
                status = _dav_status(e)
                msg = f"{op}: {e}"
                if status in (401, 403):
                    raise AuthExpired(msg) from e
                if status == 410:
                    raise CursorExpired() from e
                if status == 412:
                    raise ConflictError(msg) from e
                if status is not None and status >= 500:
                    raise TransientError(msg) from e
                raise TransientError(msg) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", op)
                raise PermanentError(f"{op}: {e}") from e

        return wrapper


def _normalise_dt(val, tzid_hint: str | None = None) -> datetime | None:
    """Coerce an icalendar `.dt` value (date or datetime) to a datetime.

    - `date` (all-day) → naive `datetime` at midnight (matches the convention
      used by `EventStore._ensure_aware_dt`, which then assumes UTC).
    - `datetime` with tzinfo → returned as-is.
    - naive `datetime` → tzinfo set from `tzid_hint` if it resolves, else UTC.
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


def _prop_dt(prop, tzid_hint: str | None = None) -> datetime | None:
    if prop is None:
        return None
    return _normalise_dt(getattr(prop, "dt", None), tzid_hint)


def _prop_dt_tuple(prop) -> tuple[datetime, ...]:
    """Flatten EXDATE / RDATE into a tuple of datetimes.

    Each property may be a single value or a list (multiple EXDATE lines).
    Each property's `.dts` is a list of vDDDTypes whose `.dt` is the value.
    Some flavors expose `.dt` directly for single values; handle both.
    """
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


def _safe(fn, *, field: str, default=None):
    """Call `fn` and return its value; on exception log and return `default`.

    Used to isolate per-field VEVENT extraction so one malformed field doesn't
    drop the whole event.
    """
    try:
        return fn()
    except Exception:
        log.exception("error extracting VEVENT field %s", field)
        return default


def _vevent_to_event(
    ve: icalendar.Event, *, calendar_id: str, href: str, etag: str
) -> Event:
    dtstart_prop = ve.get("DTSTART")
    dtstart_params = getattr(dtstart_prop, "params", None) if dtstart_prop else None
    all_day = bool(dtstart_params and dtstart_params.get("VALUE") == "DATE")
    tz = str(dtstart_params.get("TZID", "UTC")) if dtstart_params else "UTC"

    dtstart = _safe(lambda: _prop_dt(dtstart_prop, tz), field="DTSTART")

    dtend_prop = ve.get("DTEND")
    dtend = _safe(lambda: _prop_dt(dtend_prop, tz), field="DTEND")
    if dtend is None and dtstart is not None:
        duration_prop = ve.get("DURATION")
        dur = _safe(lambda: getattr(duration_prop, "dt", None), field="DURATION")
        if dur is not None:
            dtend = dtstart + dur
        elif all_day:
            dtend = dtstart + timedelta(days=1)
        else:
            dtend = dtstart

    rrule_prop = ve.get("RRULE")
    rrule = (
        _safe(lambda: rrule_prop.to_ical().decode(), field="RRULE")
        if rrule_prop is not None
        else None
    )

    exdates = _safe(
        lambda: _prop_dt_tuple(ve.get("EXDATE")), field="EXDATE", default=()
    )
    rdates = _safe(
        lambda: _prop_dt_tuple(ve.get("RDATE")), field="RDATE", default=()
    )

    attendees_raw = ve.get("ATTENDEE")
    if attendees_raw is None:
        attendees: tuple[str, ...] = ()
    else:
        items = attendees_raw if isinstance(attendees_raw, list) else [attendees_raw]
        attendees = tuple(str(a) for a in items)

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

    last_modified = _safe(
        lambda: _prop_dt(ve.get("LAST-MODIFIED")), field="LAST-MODIFIED"
    )

    return Event(
        uid=str(ve.get("UID", "")),
        calendar_id=calendar_id,
        provider_event_id=href,
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
        attendees=attendees,
        categories=categories,
        status=str(ve.get("STATUS", "CONFIRMED")),
        transparency=str(ve.get("TRANSP", "OPAQUE")),
        last_modified=last_modified,
        etag=etag,
        sequence=int(ve.get("SEQUENCE", 0)),
    )


def _discover_caldav_url(base_url: str, username: str, password: str) -> str:
    """Resolve a CalDAV base URL via /.well-known/caldav (RFC 6764).

    If the host returns a redirect from /.well-known/caldav, the redirect
    target is returned. Otherwise, the original URL is returned unchanged.
    Discovery failures (network errors, 404s, non-redirect responses) are
    swallowed so the caller can fall back to whatever the user typed.
    """
    import httpx

    if not base_url:
        return base_url
    if "://" not in base_url:
        base_url = f"https://{base_url}"

    parsed = urlparse(base_url)
    if not parsed.netloc:
        return base_url

    well_known = f"{parsed.scheme}://{parsed.netloc}/.well-known/caldav"
    try:
        auth = (username, password) if username and password else None
        with httpx.Client(auth=auth, follow_redirects=False, timeout=10.0) as client:
            resp = client.request("PROPFIND", well_known)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if location:
                    resolved = urljoin(well_known, location)
                    log.info("CalDAV discovery: %s -> %s", base_url, resolved)
                    return resolved
    except Exception:
        log.debug("CalDAV .well-known discovery failed for %s", base_url, exc_info=True)
    return base_url


def _parse_vevents(raw: str | bytes | None) -> list[icalendar.Event]:
    """Extract VEVENT components from a CalDAV event's raw iCal payload.

    CalDAV servers always wrap events in a VCALENDAR. `icalendar.Event.from_ical`
    on a VCALENDAR blob returns a `Calendar`, not an `Event` — so we must walk
    the parsed Calendar for VEVENT children. A single iCal blob can contain
    multiple VEVENTs (recurrence overrides).
    """
    if not raw:
        return []
    try:
        parsed = icalendar.Calendar.from_ical(raw)
    except Exception:
        log.warning("failed to parse caldav event ical", exc_info=True)
        return []
    if not hasattr(parsed, "walk"):
        return []
    return list(parsed.walk("VEVENT"))


def _normalise_hex_color(s: str | None) -> str | None:
    """Strip an Apple-style alpha suffix from a hex color.

    Stalwart and Apple Calendar return `#RRGGBBAA`; we keep only `#RRGGBB`.
    Invalid / empty values yield None.
    """
    if not s:
        return None
    s = s.strip()
    if not s.startswith("#"):
        return None
    if len(s) == 9:  # #RRGGBBAA → #RRGGBB
        return s[:7].lower()
    if len(s) == 7:  # #RRGGBB
        return s.lower()
    if len(s) == 4:  # #RGB → #RRGGBB
        return f"#{s[1]*2}{s[2]*2}{s[3]*2}"
    return None


def _caldav_calendar_color(cal) -> str | None:
    """Best-effort fetch of the Apple iCal `calendar-color` property."""
    try:
        from caldav.elements.ical import CalendarColor

        props = cal.get_properties([CalendarColor()])
        if not props:
            return None
        for v in props.values():
            normalised = _normalise_hex_color(str(v) if v is not None else None)
            if normalised:
                return normalised
    except Exception:
        log.debug("calendar-color fetch failed for %s", cal.url, exc_info=True)
    return None


class CalDavBackend:
    def __init__(
        self,
        account_id: str,
        server_url: str,
        username: str,
        password: str,
    ) -> None:
        self.account_id = account_id
        self._server_url = server_url
        self._username = username
        self._password = password
        self._client: caldav.DAVClient | None = None

    async def _get_client(self) -> caldav.DAVClient:
        if self._client is None:
            resolved = await asyncio.to_thread(
                _discover_caldav_url,
                self._server_url,
                self._username,
                self._password,
            )
            self._client = await asyncio.to_thread(
                lambda: caldav.DAVClient(
                    url=resolved,
                    username=self._username,
                    password=self._password,
                )
            )
        return self._client

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    def _bad_server_response(self, exc: Exception) -> PermanentError:
        return PermanentError(
            f"CalDAV server at {self._server_url!r} did not return a valid "
            "XML response. Check that the URL points to a CalDAV endpoint "
            "(not the web UI), and that the username/password are correct. "
            f"(underlying error: {exc})"
        )

    @_classify_errors
    async def list_calendars(self) -> list:
        client = await self._get_client()
        try:
            principal = await self._run(client.principal)
        except AttributeError as exc:
            # caldav.response._strip_to_multistatus tries `tree.tag` without
            # checking `tree is None`. That branch is reached when the server
            # body isn't XML at all (e.g., HTML 401 page from wrong URL/creds).
            if "tag" in str(exc):
                raise self._bad_server_response(exc) from exc
            raise
        calendars = await self._run(principal.calendars)
        result = []
        for cal in calendars:
            color = await asyncio.to_thread(_caldav_calendar_color, cal)
            result.append(
                {
                    "id": str(cal.id) if cal.id is not None else "",
                    "display_name": getattr(cal, "name", None) or str(cal.id or ""),
                    "provider_id": str(cal.url) if cal.url is not None else "",
                    "color": color,
                }
            )
        return result

    def _events_to_changes(self, events, calendar_id: str) -> list[EventChange]:
        changes: list[EventChange] = []
        for ev in events:
            href = str(ev.url) if ev.url is not None else ""
            etag = ev.etag or ""
            try:
                vevents = _parse_vevents(ev.data)
            except Exception:
                log.exception("error iterating caldav event data for %s", href)
                continue
            for ve in vevents:
                # Skip recurrence overrides: the events table's filter
                # in apply_remote_changes keys on (uid, calendar_id) so an
                # override would overwrite the master VEVENT (losing the
                # RRULE). Until the storage layer keys by recurrence_id
                # too, drop overrides — instances still expand from the
                # master and display at their original times.
                if ve.get("RECURRENCE-ID") is not None:
                    continue
                try:
                    event = _vevent_to_event(
                        ve, calendar_id=calendar_id, href=href, etag=etag
                    )
                except Exception:
                    log.exception("error mapping VEVENT for %s", href)
                    continue
                changes.append(EventChange(kind="upsert", event=event, uid=event.uid))
        return changes

    # ±1 year sync window mirrors `EventStore._instances_window_years = 1`.
    # We deliberately avoid `cal_obj.events()` (unbounded calendar-query REPORT)
    # because some servers — observed on Stalwart — return 507 Insufficient
    # Storage for the whole multistatus when a single stored event can't be
    # serialised by the server. A date-scoped `search()` produces a tighter
    # calendar-query and dodges the offending entries.
    _SYNC_WINDOW_DAYS = 365

    def _sync_window(self) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        return now - timedelta(days=self._SYNC_WINDOW_DAYS), now + timedelta(
            days=self._SYNC_WINDOW_DAYS
        )

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)
        start, end = self._sync_window()
        events = await self._run(
            lambda: cal_obj.search(start=start, end=end, event=True, expand=False)
        )
        changes = self._events_to_changes(events, calendar_id)
        yield changes, CalDavCursor(sync_token=None)

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)
        start, end = self._sync_window()
        events = await self._run(
            lambda: cal_obj.search(start=start, end=end, event=True, expand=False)
        )
        changes = self._events_to_changes(events, calendar_id)
        return changes, cursor

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)
        ve = icalendar.Event()
        ve.add("UID", event.uid)
        ve.add("SUMMARY", event.summary)
        ve.add("DTSTART", event.dtstart or datetime.utcnow())
        data = ve.to_ical().decode()
        await self._run(cal_obj.save_event, data)
        return event

    @_classify_errors
    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        return event

    @_classify_errors
    async def delete_event(
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None:
        pass
