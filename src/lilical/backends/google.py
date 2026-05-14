from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any, AsyncIterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from lilical.backends.base import (
    AuthExpired,
    Backend,
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
CLIENT_CONFIG = {
    "installed": {
        "client_id": "lilical-oauth",
        "client_secret": "",
        "redirect_uris": ["http://127.0.0.1"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


class GoogleCursor(SyncCursor):
    def __init__(self, sync_token: str | None = None) -> None:
        self.sync_token = sync_token

    def to_json(self) -> dict:
        return {"sync_token": self.sync_token}

    @classmethod
    def from_json(cls, data: dict) -> GoogleCursor:
        return cls(sync_token=data.get("sync_token"))


def _classify_errors(f):
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


def _google_event_to_change(ev_json: dict, calendar_id: str) -> EventChange:
    status = ev_json.get("status", "")
    if status == "cancelled":
        return EventChange(
            kind="delete",
            uid=ev_json.get("iCalUID", ev_json.get("id", "")),
        )
    uid = ev_json.get("iCalUID", ev_json.get("id", ""))
    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=ev_json.get("id"),
        summary=ev_json.get("summary", ""),
        description=ev_json.get("description", ""),
        location=ev_json.get("location", ""),
        url=ev_json.get("htmlLink"),
        etag=ev_json.get("etag"),
        sequence=ev_json.get("sequence", 0),
        status="CONFIRMED" if status == "confirmed" else status.upper(),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


class GoogleBackend:
    def __init__(self, account_id: str, token_json: str | None = None) -> None:
        self.account_id = account_id
        self._token_json = token_json
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

    def _get_service(self):
        if self._service is not None:
            return self._service
        creds = self._get_credentials()
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    @_classify_errors
    async def list_calendars(self) -> list:
        service = self._get_service()
        resp = service.calendarList().list().execute()
        return [
            {
                "id": cal["id"],
                "display_name": cal.get("summary", cal["id"]),
                "provider_id": cal["id"],
            }
            for cal in resp.get("items", [])
        ]

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        service = self._get_service()
        req = service.events().list(
            calendarId=calendar_id,
            singleEvents=False,
            showDeleted=True,
            maxResults=250,
        )
        while req is not None:
            resp = req.execute()
            changes = [
                _google_event_to_change(ev, calendar_id)
                for ev in resp.get("items", [])
            ]
            sync_token = resp.get("nextSyncToken")
            if "nextPageToken" in resp:
                req = service.events().list_next(req, resp)
                yield changes, GoogleCursor()
            else:
                yield changes, GoogleCursor(sync_token=sync_token)
                req = None

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        service = self._get_service()
        sync_token = cursor.to_json().get("sync_token")
        if not sync_token:
            raise CursorExpired(calendar_id)
        req = service.events().list(
            calendarId=calendar_id,
            syncToken=sync_token,
            singleEvents=False,
            showDeleted=True,
        )
        resp = req.execute()
        changes = [
            _google_event_to_change(ev, calendar_id)
            for ev in resp.get("items", [])
        ]
        new_token = resp.get("nextSyncToken", sync_token)
        return changes, GoogleCursor(sync_token=new_token)

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        service = self._get_service()
        body: dict[str, Any] = {
            "summary": event.summary,
        }
        resp = service.events().insert(
            calendarId=calendar_id, body=body, sendUpdates="none"
        ).execute()
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
        service = self._get_service()
        body: dict[str, Any] = {
            "summary": event.summary,
        }
        resp = service.events().update(
            calendarId=calendar_id,
            eventId=event.provider_event_id,
            body=body,
            sendUpdates="none",
        ).execute()
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
        service = self._get_service()
        service.events().delete(
            calendarId=calendar_id, eventId=uid
        ).execute()
