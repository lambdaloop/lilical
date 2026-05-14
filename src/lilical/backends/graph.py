from __future__ import annotations

import functools
import logging
from typing import Any, AsyncIterator

from azure.identity import InteractiveBrowserCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.item.calendars.item.calendar_view.delta.delta_request_builder import (
    DeltaRequestBuilder,
)

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

GRAPH_CLIENT_ID = "lilical-graph"


class GraphCursor(SyncCursor):
    def __init__(self, delta_link: str | None = None) -> None:
        self.delta_link = delta_link

    def to_json(self) -> dict:
        return {"delta_link": self.delta_link}

    @classmethod
    def from_json(cls, data: dict) -> GraphCursor:
        return cls(delta_link=data.get("delta_link"))


def _classify_errors(f):
    @functools.wraps(f)
    async def wrapper(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "unauthorized" in err_str:
                raise AuthExpired(str(e)) from e
            if "410" in err_str:
                raise CursorExpired() from e
            if "412" in err_str or "conflict" in err_str:
                raise ConflictError(str(e)) from e
            if any(s in err_str for s in ("429", "timeout", "500", "503")):
                raise TransientError(str(e)) from e
            raise PermanentError(str(e)) from e
    return wrapper


def _graph_event_to_change(ev_json: dict, calendar_id: str) -> EventChange:
    uid = ev_json.get("iCalUId", ev_json.get("id", ""))
    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=ev_json.get("id"),
        summary=ev_json.get("subject", ""),
        description=ev_json.get("body", {}).get("content", ""),
        location=ev_json.get("location", {}).get("displayName", ""),
        etag=ev_json.get("@odata.etag"),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


class GraphBackend:
    def __init__(
        self, account_id: str, token_json: str | None = None
    ) -> None:
        self.account_id = account_id
        self._token_json = token_json
        self._client: GraphServiceClient | None = None
        self._credential: InteractiveBrowserCredential | None = None

    def _get_client(self) -> GraphServiceClient:
        if self._client is not None:
            return self._client
        self._credential = InteractiveBrowserCredential(
            client_id=GRAPH_CLIENT_ID,
            tenant_id="common",
            redirect_uri="http://localhost",
        )
        self._client = GraphServiceClient(credentials=self._credential)
        return self._client

    @_classify_errors
    async def list_calendars(self) -> list:
        client = self._get_client()
        me = await client.users.get()
        user_id = me.id if me else "me"
        calendars = await client.users.by_user_id(user_id).calendars.get()
        return [
            {
                "id": cal.id,
                "display_name": cal.name or cal.id or "",
                "provider_id": cal.id or "",
            }
            for cal in calendars.value if calendars and calendars.value
        ]

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        client = self._get_client()
        me = await client.users.get()
        user_id = me.id if me else "me"
        req = client.users.by_user_id(user_id).calendars.by_calendar_id(
            calendar_id
        ).events
        result = await req.get()
        changes: list[EventChange] = []
        if result and result.value:
            for ev in result.value:
                changes.append(
                    _graph_event_to_change(
                        {"id": ev.id, "subject": ev.subject or ""}, calendar_id
                    )
                )
        yield changes, GraphCursor()

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        return [], cursor

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
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
