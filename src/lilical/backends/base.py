from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from lilical.models.contact import Contact
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

    async def rename_calendar(self, calendar_id: str, new_name: str) -> None:
        """Rename *calendar_id* on the provider server.

        Raises PermanentError for permission-denied / read-only.
        Raises AuthExpired if the session needs re-authentication.
        Raises TransientError for recoverable network/server issues.
        """
        ...

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

    def supported_contact_sources(self) -> tuple[str, ...]:
        """Return which contact source keys this backend provides."""
        return ()

    async def list_contacts(
        self, source: str, cursor: dict | None
    ) -> tuple[list[Contact], dict | None, bool]:
        """Fetch a page of contacts for *source*.

        Returns (contacts, next_cursor, is_complete).
        *next_cursor* is None when there are no more pages.
        *is_complete* is True when the full refresh for this source is done.
        Backends that don't implement a given source return ([], None, True).
        """
        return [], None, True
