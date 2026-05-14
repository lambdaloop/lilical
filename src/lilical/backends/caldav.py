from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime
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


def _classify_errors(f):
    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except AuthorizationError as e:
                raise AuthExpired(str(e)) from e
            except DAVError as e:
                if e.response.status in (401, 403):
                    raise AuthExpired(str(e)) from e
                if e.response.status == 410:
                    raise CursorExpired() from e
                if e.response.status == 412:
                    raise ConflictError(str(e)) from e
                if e.response.status >= 500:
                    raise TransientError(str(e)) from e
                raise TransientError(str(e)) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", f.__name__)
                raise PermanentError(str(e)) from e

        return wrapper
    else:

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except AuthorizationError as e:
                raise AuthExpired(str(e)) from e
            except DAVError as e:
                if e.response.status in (401, 403):
                    raise AuthExpired(str(e)) from e
                if e.response.status == 410:
                    raise CursorExpired() from e
                if e.response.status == 412:
                    raise ConflictError(str(e)) from e
                if e.response.status >= 500:
                    raise TransientError(str(e)) from e
                raise TransientError(str(e)) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", f.__name__)
                raise PermanentError(str(e)) from e

        return wrapper


def _vevent_to_event(
    ve: icalendar.Event, *, calendar_id: str, href: str, etag: str
) -> Event:
    dtstart_prop = ve.get("DTSTART")
    dtstart_params = getattr(dtstart_prop, "params", None) if dtstart_prop else None
    all_day = bool(dtstart_params and dtstart_params.get("VALUE") == "DATE")
    return Event(
        uid=str(ve.get("UID", "")),
        calendar_id=calendar_id,
        provider_event_id=href,
        summary=str(ve.get("SUMMARY", "")),
        description=str(ve.get("DESCRIPTION", "")),
        location=str(ve.get("LOCATION", "")),
        etag=etag,
        sequence=int(ve.get("SEQUENCE", 0)),
        all_day=all_day,
        tz=str(dtstart_params.get("TZID", "UTC")) if dtstart_params else "UTC",
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
        return [
            {
                "id": str(cal.id) if cal.id is not None else "",
                "display_name": getattr(cal, "name", None) or str(cal.id or ""),
                "provider_id": str(cal.url) if cal.url is not None else "",
            }
            for cal in calendars
        ]

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
                try:
                    event = _vevent_to_event(
                        ve, calendar_id=calendar_id, href=href, etag=etag
                    )
                except Exception:
                    log.exception("error mapping VEVENT for %s", href)
                    continue
                changes.append(EventChange(kind="upsert", event=event, uid=event.uid))
        return changes

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)
        events = await self._run(cal_obj.events)
        changes = self._events_to_changes(events, calendar_id)
        yield changes, CalDavCursor(sync_token=None)

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)
        events = await self._run(cal_obj.events)
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
