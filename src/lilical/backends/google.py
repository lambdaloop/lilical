from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
from typing import Any, AsyncIterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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


class GoogleCursor(SyncCursor):
    def __init__(self, sync_token: str | None = None) -> None:
        self.sync_token = sync_token

    def to_json(self) -> dict:
        return {"sync_token": self.sync_token}

    @classmethod
    def from_json(cls, data: dict) -> GoogleCursor:
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
                all_changes.append(_google_event_to_change(ev, calendar_id))
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
        sync_token = cursor.to_json().get("sync_token")
        if not sync_token:
            raise CursorExpired(calendar_id)
        req = service.events().list(
            calendarId=calendar_id,
            syncToken=sync_token,
            singleEvents=False,
            showDeleted=True,
        )
        resp = await self._execute(req)
        changes = [
            _google_event_to_change(ev, calendar_id) for ev in resp.get("items", [])
        ]
        new_token = resp.get("nextSyncToken", sync_token)
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
