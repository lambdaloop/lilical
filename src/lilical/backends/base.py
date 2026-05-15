from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from lilical.models.event import Event


class SyncCursor(Protocol):
    def to_json(self) -> dict[str, object]: ...
    @classmethod
    def from_json(cls, data: dict[str, object]) -> SyncCursor: ...


@dataclass(frozen=True, slots=True)
class EventChange:
    kind: Literal["upsert", "delete"]
    event: Event | None = None
    uid: str = ""


class CursorExpired(Exception):  # noqa: N818
    def __init__(self, calendar_id: str = "") -> None:
        self.calendar_id = calendar_id
        super().__init__(f"Cursor expired for calendar {calendar_id}")


class AuthExpired(Exception):  # noqa: N818
    pass


class ConflictError(Exception):
    pass


class TransientError(Exception):
    pass


class PermanentError(Exception):
    pass


class Backend(Protocol):
    account_id: str

    async def list_calendars(self) -> list[dict[str, object]]: ...

    def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]: ...

    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]: ...

    async def create_event(self, calendar_id: str, event: Event) -> Event: ...

    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event: ...

    async def delete_event(
        self, calendar_id: str, provider_event_id: str, if_match: str | None
    ) -> None: ...

    async def update_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: "datetime",
        event: Event,
    ) -> None: ...

    async def delete_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: "datetime",
    ) -> None: ...

    async def respond_to_event(
        self, calendar_id: str, event: Event, response: str
    ) -> "Event | None":
        """Send an RSVP response for an event the user is invited to.

        `response` must be one of "ACCEPTED", "TENTATIVE", or "DECLINED".
        Returns the updated canonical Event (refreshed etag/sequence) when the
        backend echoes state, or None when it does not (e.g. Graph's 202).
        """
        ...
