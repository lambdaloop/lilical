from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import zoneinfo
from datetime import date as _date_cls
from datetime import datetime, time, timezone
from typing import Any, AsyncIterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from lilical.utils.timezone import local_iana_tz, local_zoneinfo
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

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]


def _load_client_config() -> dict:
    client_id = os.environ.get("LILICAL_GOOGLE_CLIENT_ID") or "lilical-oauth"
    client_secret = os.environ.get("LILICAL_GOOGLE_CLIENT_SECRET") or ""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://127.0.0.1"],
            "auth_uri": os.environ.get(
                "LILICAL_GOOGLE_AUTH_URI",
                "https://accounts.google.com/o/oauth2/auth",
            ),
            "token_uri": os.environ.get(
                "LILICAL_GOOGLE_TOKEN_URI",
                "https://oauth2.googleapis.com/token",
            ),
        }
    }


CLIENT_CONFIG = _load_client_config()


# Map Google Calendar responseStatus → our normalized vocabulary.
_GOOGLE_RESPONSE_MAP: dict[str, str] = {
    "accepted": "ACCEPTED",
    "tentative": "TENTATIVE",
    "declined": "DECLINED",
    "needsaction": "NEEDS-ACTION",
}


class GoogleCursor(SyncCursor):
    _TYPE = "google"

    def __init__(self, sync_token: str | None = None) -> None:
        self.sync_token = sync_token

    def to_json(self) -> dict:
        return {"_type": self._TYPE, "sync_token": self.sync_token}

    @classmethod
    def from_json(cls, data: dict) -> GoogleCursor:
        if data.get("_type") != cls._TYPE:
            raise ValueError(f"not a google cursor: {data!r}")
        return cls(sync_token=data.get("sync_token"))


def _classify_errors(f):
    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except HttpError as e:
                if e.resp.status in (401, 403):
                    raise AuthExpired(str(e)) from e
                if e.resp.status == 410:
                    raise CursorExpired() from e
                if e.resp.status == 412:
                    raise ConflictError(str(e)) from e
                if e.resp.status >= 500 or e.resp.status == 429:
                    raise TransientError(str(e)) from e
                raise PermanentError(str(e)) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified error in %s", f.__name__)
                raise PermanentError(str(e)) from e

        return wrapper
    else:

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except HttpError as e:
                if e.resp.status in (401, 403):
                    raise AuthExpired(str(e)) from e
                if e.resp.status == 410:
                    raise CursorExpired() from e
                if e.resp.status == 412:
                    raise ConflictError(str(e)) from e
                if e.resp.status >= 500 or e.resp.status == 429:
                    raise TransientError(str(e)) from e
                raise PermanentError(str(e)) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified error in %s", f.__name__)
                raise PermanentError(str(e)) from e

        return wrapper


def _parse_google_dt(part: dict | None) -> tuple[datetime | None, str, bool]:
    """Resolve a Google start/end object to (datetime, tz, all_day).

    Google's start/end is either:
      - {"date": "YYYY-MM-DD"}  → all-day, returned as naive midnight datetime
        so EventStore._ensure_aware_dt treats it as UTC midnight.
      - {"dateTime": "...", "timeZone": "America/Los_Angeles"}  → timed; if the
        parsed dateTime is naive, the timeZone field is applied.
    """
    if not part:
        return None, "UTC", False
    if "date" in part:
        try:
            d = _date_cls.fromisoformat(part["date"])
            local = local_zoneinfo()
            return datetime.combine(d, time.min, tzinfo=local), local_iana_tz(), True
        except ValueError:
            return None, local_iana_tz(), True
    raw = part.get("dateTime")
    tz = str(part.get("timeZone") or local_iana_tz())
    if not raw:
        return None, tz, False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, tz, False
    if dt.tzinfo is not None:
        return dt, tz, False
    if tz and tz != "UTC":
        try:
            return dt.replace(tzinfo=zoneinfo.ZoneInfo(tz)), tz, False
        except Exception:
            pass
    return dt.replace(tzinfo=timezone.utc), tz, False


def _parse_recurrence_lines(lines: list[str]) -> tuple[str | None, tuple, tuple]:
    """Split Google's `recurrence` array into (rrule, exdates, rdates).

    Google formats recurrence as iCal property strings:
      "RRULE:FREQ=WEEKLY;BYDAY=MO"
      "EXDATE;TZID=America/Los_Angeles:20260527T090000"
      "RDATE:20260601T090000Z"
    We need the RRULE value (without the prefix) for RecurrenceExpander, and
    EXDATE/RDATE values as datetime tuples.
    """
    import icalendar

    rrule_val: str | None = None
    exdates: list[datetime] = []
    rdates: list[datetime] = []
    for raw in lines or []:
        if not isinstance(raw, str):
            continue
        try:
            tag, rest = raw.split(":", 1) if ":" in raw else (raw, "")
        except Exception:
            continue
        name = tag.split(";", 1)[0].upper()
        if name == "RRULE" and rrule_val is None:
            rrule_val = rest
            continue
        if name in ("EXDATE", "RDATE"):
            try:
                # icalendar parses the full property; we only need its value list.
                cal = icalendar.Calendar.from_ical(
                    f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nDTSTART:20260101T000000Z\n{raw}\nEND:VEVENT\nEND:VCALENDAR\n"
                )
                vevents = list(cal.walk("VEVENT"))
                if not vevents:
                    continue
                prop = vevents[0].get(name)
                if prop is None:
                    continue
                items = prop if isinstance(prop, list) else [prop]
                bucket = exdates if name == "EXDATE" else rdates
                for p in items:
                    dts = getattr(p, "dts", None) or []
                    for entry in dts:
                        val = getattr(entry, "dt", None)
                        if isinstance(val, datetime):
                            bucket.append(
                                val if val.tzinfo else val.replace(tzinfo=timezone.utc)
                            )
                        elif isinstance(val, _date_cls):
                            bucket.append(datetime.combine(val, time.min))
            except Exception:
                log.exception("Google: failed to parse recurrence line %r", raw)
    return rrule_val, tuple(exdates), tuple(rdates)


def _google_event_to_change(ev_json: dict, calendar_id: str) -> EventChange | None:
    status = ev_json.get("status", "")
    if status == "cancelled":
        return EventChange(
            kind="delete",
            uid=ev_json.get("iCalUID", ev_json.get("id", "")),
        )
    # Skip recurrence overrides (modified instances of a recurring series).
    # The storage layer keys events by (uid, calendar_id) and doesn't yet
    # distinguish overrides — accepting them would clobber the master and we'd
    # lose the RRULE.
    if ev_json.get("recurringEventId"):
        return None

    uid = ev_json.get("iCalUID", ev_json.get("id", ""))

    dtstart, tz_start, all_day_start = _parse_google_dt(ev_json.get("start"))
    dtend, _tz_end, _all_day_end = _parse_google_dt(ev_json.get("end"))
    all_day = all_day_start

    rrule, exdates, rdates = _parse_recurrence_lines(ev_json.get("recurrence") or [])

    g_status = (
        "CONFIRMED"
        if status == "confirmed"
        else (status.upper() if status else "CONFIRMED")
    )
    transparency = (
        "TRANSPARENT" if ev_json.get("transparency") == "transparent" else "OPAQUE"
    )

    attendees_raw = ev_json.get("attendees") or []
    attendees: list[str] = []
    self_response: str | None = None
    for a in attendees_raw:
        if not isinstance(a, dict):
            continue
        if a.get("email"):
            attendees.append(str(a["email"]))
        if a.get("self") is True:
            self_response = _GOOGLE_RESPONSE_MAP.get(
                str(a.get("responseStatus") or "").lower()
            )

    last_modified: datetime | None = None
    updated_raw = ev_json.get("updated")
    if isinstance(updated_raw, str):
        try:
            last_modified = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=ev_json.get("id"),
        dtstart=dtstart,
        dtend=dtend,
        tz=tz_start,
        all_day=all_day,
        summary=ev_json.get("summary", ""),
        description=ev_json.get("description", ""),
        location=ev_json.get("location", ""),
        url=ev_json.get("htmlLink"),
        rrule=rrule,
        exdates=exdates,
        rdates=rdates,
        attendees=tuple(attendees),
        status=g_status,
        self_response=self_response,
        transparency=transparency,
        last_modified=last_modified,
        etag=ev_json.get("etag"),
        sequence=ev_json.get("sequence", 0),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


async def run_google_oauth_flow() -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow

    _validate_client_config()
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
    creds = await asyncio.to_thread(
        lambda: flow.run_local_server(open_browser=True, timeout_seconds=300)
    )
    return creds.to_json()


def _validate_client_config() -> None:
    cid = CLIENT_CONFIG.get("installed", {}).get("client_id", "")
    if not cid or cid == "lilical-oauth":
        raise RuntimeError(
            "Google OAuth is not configured.\n\n"
            "Set the LILICAL_GOOGLE_CLIENT_ID environment variable to your\n"
            "Google OAuth 2.0 client ID (desktop application type).\n\n"
            "To create one:\n"
            "  1. Go to https://console.cloud.google.com/apis/credentials\n"
            "  2. Create an OAuth 2.0 Client ID (Desktop app)\n"
            "  3. Add http://127.0.0.1 to Authorized Redirect URIs\n"
            "  4. Set the environment variable and restart lilical\n\n"
            "  export LILICAL_GOOGLE_CLIENT_ID='xxxx.apps.googleusercontent.com'"
        )


class GoogleBackend:
    def __init__(
        self,
        account_id: str,
        token_json: str | None = None,
        on_token_refreshed=None,
    ) -> None:
        self.account_id = account_id
        self._token_json = token_json
        self._on_token_refreshed = on_token_refreshed
        self._creds: Credentials | None = None
        self._service: Any = None

    def _get_credentials(self) -> Credentials:
        if self._creds is not None:
            return self._creds
        if self._token_json:
            self._creds = Credentials.from_authorized_user_info(
                json.loads(self._token_json), SCOPES
            )
        else:
            self._creds = None
        return self._creds

    def _set_credentials(self, creds: Credentials) -> None:
        self._creds = creds
        self._token_json = creds.to_json()
        self._service = None

    async def _ensure_service(self):
        if self._service is not None:
            return self._service
        creds = self._get_credentials()
        if creds and creds.expired and creds.refresh_token:
            await asyncio.to_thread(creds.refresh, Request())
            self._token_json = creds.to_json()
            if self._on_token_refreshed:
                self._on_token_refreshed(self._token_json)
        self._service = await asyncio.to_thread(
            lambda: build("calendar", "v3", credentials=creds, cache_discovery=False)
        )
        return self._service

    async def _execute(self, request):
        return await asyncio.to_thread(request.execute)

    def get_token_json(self) -> str | None:
        return self._token_json

    @_classify_errors
    async def list_calendars(self) -> list:
        service = await self._ensure_service()
        resp = await self._execute(service.calendarList().list())
        out = []
        for cal in resp.get("items", []):
            # Google returns backgroundColor as a `#rrggbb` hex string.
            colour = cal.get("backgroundColor")
            if colour and not colour.startswith("#"):
                colour = "#" + colour
            out.append(
                {
                    "id": cal["id"],
                    "display_name": cal.get("summary", cal["id"]),
                    "provider_id": cal["id"],
                    "color": (colour or "").lower() or None,
                }
            )
        return out

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        service = await self._ensure_service()
        req = service.events().list(
            calendarId=calendar_id,
            singleEvents=False,
            showDeleted=True,
            maxResults=250,
        )
        all_changes: list[EventChange] = []
        while req is not None:
            resp = await self._execute(req)
            for ev in resp.get("items", []):
                change = _google_event_to_change(ev, calendar_id)
                if change is not None:
                    all_changes.append(change)
            if "nextPageToken" in resp:
                req = service.events().list_next(req, resp)
            else:
                sync_token = resp.get("nextSyncToken")
                yield all_changes, GoogleCursor(sync_token=sync_token)
                req = None

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        service = await self._ensure_service()
        if not isinstance(cursor, GoogleCursor) or not cursor.sync_token:
            raise CursorExpired(calendar_id)
        sync_token = cursor.sync_token
        req = service.events().list(
            calendarId=calendar_id,
            syncToken=sync_token,
            singleEvents=False,
            showDeleted=True,
            maxResults=250,
        )
        changes: list[EventChange] = []
        new_token = sync_token
        while req is not None:
            resp = await self._execute(req)
            for ev in resp.get("items", []):
                change = _google_event_to_change(ev, calendar_id)
                if change is not None:
                    changes.append(change)
            if "nextPageToken" in resp:
                req = service.events().list_next(req, resp)
            else:
                new_token = resp.get("nextSyncToken", sync_token)
                req = None
        return changes, GoogleCursor(sync_token=new_token)

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        service = await self._ensure_service()
        body: dict[str, Any] = {
            "summary": event.summary,
        }
        resp = await self._execute(
            service.events().insert(
                calendarId=calendar_id, body=body, sendUpdates="none"
            )
        )
        return Event(
            uid=resp.get("iCalUID", resp["id"]),
            calendar_id=calendar_id,
            provider_event_id=resp["id"],
            summary=resp.get("summary", ""),
            etag=resp.get("etag"),
        )

    @_classify_errors
    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        service = await self._ensure_service()
        body: dict[str, Any] = {
            "summary": event.summary,
        }
        resp = await self._execute(
            service.events().update(
                calendarId=calendar_id,
                eventId=event.provider_event_id,
                body=body,
                sendUpdates="none",
            )
        )
        return Event(
            uid=resp.get("iCalUID", resp["id"]),
            calendar_id=calendar_id,
            provider_event_id=resp["id"],
            summary=resp.get("summary", ""),
            etag=resp.get("etag"),
        )

    @_classify_errors
    async def delete_event(
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None:
        service = await self._ensure_service()
        await self._execute(
            service.events().delete(calendarId=calendar_id, eventId=uid)
        )
