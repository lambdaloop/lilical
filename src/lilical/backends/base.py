from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol

from lilical.models.event import Event


class SyncCursor(Protocol):
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> SyncCursor: ...


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

    async def list_calendars(self) -> list: ...

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
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None: ...
