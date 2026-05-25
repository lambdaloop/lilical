from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from lilical.backends.base import (
    EventChange,
    PermanentError,
    SyncCursor,
)
from lilical.ics.fetch import fetch_ics
from lilical.ics.importer import parse_ics_to_events
from lilical.models.event import Event

if TYPE_CHECKING:
    from lilical.models.contact import Contact
    from lilical.storage.event_store import EventStore

log = logging.getLogger(__name__)

SUBSCRIPTION_ACCOUNT_ID = "subscriptions"
SUBSCRIPTION_ACCOUNT_NAME = "Subscriptions"


@dataclass(frozen=True, slots=True)
class SubscriptionCursor(SyncCursor):
    _TYPE = "subscription"

    etag: str | None = None
    last_modified: str | None = None
    content_sha256: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "_type": self._TYPE,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> SubscriptionCursor:
        if data.get("_type") != cls._TYPE:
            raise ValueError(f"not a subscription cursor: {data!r}")
        return cls(
            etag=data.get("etag"),  # type: ignore[reportArgumentType]
            last_modified=data.get("last_modified"),  # type: ignore[reportArgumentType]
            content_sha256=str(data.get("content_sha256", "")),
        )


def _event_signature(event: Event) -> str:
    """Stable SHA-256 hash of an event's user-visible content fields.

    Used by incremental_sync to skip upserts for events whose content is
    byte-identical to the already-stored version.
    """
    parts = {
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "dtstart": event.dtstart.isoformat() if event.dtstart else None,
        "dtend": event.dtend.isoformat() if event.dtend else None,
        "tz": event.tz,
        "all_day": event.all_day,
        "url": event.url,
        "rrule": event.rrule,
        "recurrence_id": (
            event.recurrence_id.isoformat() if event.recurrence_id else None
        ),
        "exdates": [dt.isoformat() for dt in event.exdates],
        "rdates": [dt.isoformat() for dt in event.rdates],
        "status": event.status,
        "transparency": event.transparency,
        "color": event.color,
        "categories": sorted(event.categories),
        "organizer": dataclasses.asdict(event.organizer) if event.organizer else None,
        "attendees": sorted(
            [dataclasses.asdict(a) for a in event.attendees],
            key=lambda x: x.get("email", ""),
        ),
    }
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _read_only_error() -> PermanentError:
    return PermanentError("subscription is read-only")


class SubscriptionBackend:
    """Read-only backend that mirrors external ICS feeds as calendars.

    Each Calendar row under the singleton "subscriptions" account stores its
    source URL in `provider_id`. `initial_sync` and `incremental_sync` fetch
    the feed (with conditional GET / mtime check), parse it, and diff against
    the current event UIDs in the EventStore for that calendar.
    """

    def __init__(self, *, account_id: str, store: EventStore) -> None:
        self.account_id = account_id
        self._store = store

    async def list_calendars(self) -> list[dict[str, object]]:
        # Subscriptions are user-managed via the SubscribeDialog; this method
        # just echoes back what's already in the DB so the engine's tick path
        # finds and syncs each one. upsert_calendars only inserts/updates —
        # it never deletes calendars absent from this list.
        cals = await asyncio.to_thread(
            self._store.list_calendars, self.account_id, False
        )
        return [
            {
                "provider_id": c.provider_id,
                "display_name": c.display_name,
                "color": c.color,
            }
            for c in cals
        ]

    async def _fetch_and_parse(
        self,
        source: str,
        calendar_id: str,
        prev_etag: str | None,
        prev_last_modified: str | None,
        prev_sha256: str | None,
    ) -> tuple[list[Event], SubscriptionCursor, bool]:
        """Fetch + parse the source. Returns (events, new_cursor, changed)."""
        body, etag, last_modified = await fetch_ics(
            source,
            prev_etag=prev_etag,
            prev_last_modified=prev_last_modified,
        )
        if body is None:
            # 304 / unchanged mtime — keep previous content_sha256 to preserve
            # the diff baseline.
            return (
                [],
                SubscriptionCursor(
                    etag=etag,
                    last_modified=last_modified,
                    content_sha256=prev_sha256 or "",
                ),
                False,
            )
        sha = hashlib.sha256(body).hexdigest()
        if prev_sha256 and sha == prev_sha256:
            return (
                [],
                SubscriptionCursor(
                    etag=etag, last_modified=last_modified, content_sha256=sha
                ),
                False,
            )
        events, _calname = await asyncio.to_thread(
            parse_ics_to_events, body, calendar_id
        )
        return (
            events,
            SubscriptionCursor(
                etag=etag, last_modified=last_modified, content_sha256=sha
            ),
            True,
        )

    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        # calendar_id here is the provider_id (canonical source URL).
        source = calendar_id
        local_cal = await asyncio.to_thread(self._lookup_local_cal_id, source)
        events, cursor, _ = await self._fetch_and_parse(
            source, local_cal or "", None, None, None
        )
        changes = [EventChange(kind="upsert", event=e, uid=e.uid) for e in events]
        yield changes, cursor

    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        if not isinstance(cursor, SubscriptionCursor):
            raise PermanentError("expected SubscriptionCursor")
        source = calendar_id
        local_cal = await asyncio.to_thread(self._lookup_local_cal_id, source)
        events, new_cursor, changed = await self._fetch_and_parse(
            source,
            local_cal or "",
            cursor.etag,
            cursor.last_modified,
            cursor.content_sha256,
        )
        if not changed:
            return [], new_cursor

        new_uids = {e.uid for e in events}
        existing_sigs: dict[tuple[str, str], str] = (
            await asyncio.to_thread(self._list_event_signatures, local_cal)
            if local_cal
            else {}
        )
        existing_uids = {k[0] for k in existing_sigs}
        changes: list[EventChange] = []
        for e in events:
            rec_id = e.recurrence_id.isoformat() if e.recurrence_id else ""
            if existing_sigs.get((e.uid, rec_id)) != _event_signature(e):
                changes.append(EventChange(kind="upsert", event=e, uid=e.uid))
        for uid in existing_uids - new_uids:
            changes.append(EventChange(kind="delete", uid=uid))
        return changes, new_cursor

    def _lookup_local_cal_id(self, source: str) -> str | None:
        for cal in self._store.list_calendars(self.account_id, included_only=False):
            if cal.provider_id == source:
                return cal.id
        return None

    def _list_event_signatures(
        self, calendar_id: str
    ) -> dict[tuple[str, str], str]:
        from sqlalchemy.orm import Session

        from lilical.models.event import EventRow

        with Session(self._store._engine) as s:  # type: ignore[reportPrivateUsage]
            rows = s.query(EventRow).filter(EventRow.calendar_id == calendar_id).all()

        result: dict[tuple[str, str], str] = {}
        for row in rows:
            exdates = json.loads(row.exdates) if row.exdates else []
            rdates = json.loads(row.rdates) if row.rdates else []
            attendees = json.loads(row.attendees) if row.attendees else []
            organizer = json.loads(row.organizer) if row.organizer else None
            categories = json.loads(row.categories) if row.categories else []
            parts = {
                "summary": row.summary or "",
                "description": row.description or "",
                "location": row.location or "",
                "dtstart": row.dtstart or None,
                "dtend": row.dtend or None,
                "tz": row.tz or "UTC",
                "all_day": bool(row.all_day),
                "url": row.url,
                "rrule": row.rrule,
                "recurrence_id": row.recurrence_id or None,
                "exdates": exdates,
                "rdates": rdates,
                "status": row.status or "CONFIRMED",
                "transparency": row.transparency or "OPAQUE",
                "color": row.color,
                "categories": sorted(categories),
                "organizer": organizer,
                "attendees": sorted(attendees, key=lambda x: x.get("email", "")),
            }
            blob = json.dumps(parts, sort_keys=True, default=str).encode()
            sig = hashlib.sha256(blob).hexdigest()
            result[(row.uid, row.recurrence_id or "")] = sig
        return result

    # ── Write methods: all read-only ─────────────────────────────────────────
    async def rename_calendar(self, calendar_id: str, new_name: str) -> None:
        raise _read_only_error()

    async def create_calendar(self, name: str) -> dict[str, object]:
        raise _read_only_error()

    async def delete_calendar(self, calendar_id: str) -> None:
        raise _read_only_error()

    async def create_event(self, calendar_id: str, event: Event) -> Event:
        raise _read_only_error()

    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        raise _read_only_error()

    async def delete_event(
        self, calendar_id: str, provider_event_id: str, if_match: str | None
    ) -> None:
        raise _read_only_error()

    async def update_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: datetime,
        event: Event,
    ) -> None:
        raise _read_only_error()

    async def delete_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: datetime,
    ) -> None:
        raise _read_only_error()

    async def respond_to_event(
        self, calendar_id: str, event: Event, response: str
    ) -> Event | None:
        raise _read_only_error()

    def supported_contact_sources(self) -> tuple[str, ...]:
        return ()

    async def list_contacts(
        self, source: str, cursor: dict | None
    ) -> tuple[list[Contact], dict | None, bool]:
        return [], None, True
