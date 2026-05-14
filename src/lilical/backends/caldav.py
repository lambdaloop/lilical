from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

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
    all_day = dtstart_prop.params.get("VALUE") == "DATE" if dtstart_prop else False
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
        tz=str(dtstart_prop.params.get("TZID", "UTC")) if dtstart_prop else "UTC",
    )


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

    def _get_client(self) -> caldav.DAVClient:
        if self._client is None:
            self._client = caldav.DAVClient(
                url=self._server_url,
                username=self._username,
                password=self._password,
            )
        return self._client

    @_classify_errors
    async def list_calendars(self) -> list:
        client = self._get_client()
        principal = client.principal()
        calendars = principal.calendars()
        return [
            {
                "id": cal.id,
                "display_name": getattr(cal, "name", cal.id),
                "provider_id": cal.url,
            }
            for cal in calendars
        ]

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        client = self._get_client()
        cal = caldav.Calendar(client=client, url=calendar_id)
        events = cal.events()
        changes: list[EventChange] = []
        for ev in events:
            ve = icalendar.Event.from_ical(ev.data)
            event = _vevent_to_event(
                ve, calendar_id=calendar_id, href=ev.url, etag=ev.etag or ""
            )
            changes.append(EventChange(kind="upsert", event=event, uid=event.uid))
        cursor = CalDavCursor(sync_token=None)
        yield changes, cursor

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        return [], cursor

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        cal_obj = caldav.Calendar(
            client=self._get_client(), url=calendar_id
        )
        ve = icalendar.Event()
        ve.add("UID", event.uid)
        ve.add("SUMMARY", event.summary)
        ve.add("DTSTART", event.dtstart or datetime.utcnow())
        data = ve.to_ical().decode()
        cal_obj.save_event(data)
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
