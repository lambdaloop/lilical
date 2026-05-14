from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from PySide6.QtCore import QObject, Signal

from lilical.models.event import EventRow, EventInstanceRow, Event


def _utc_now() -> str:
    return datetime.utcnow().isoformat()


def _row_to_event(row: EventRow) -> Event:
    return Event(
        uid=row.uid,
        calendar_id=row.calendar_id,
        provider_event_id=row.provider_event_id,
        summary=row.summary or "",
        description=row.description or "",
        location=row.location or "",
        status=row.status or "CONFIRMED",
        transparency=row.transparency or "OPAQUE",
        etag=row.etag,
        sequence=row.sequence or 0,
        local_dirty=bool(row.local_dirty),
        deleted_locally=bool(row.deleted_locally),
        conflict_state=row.conflict_state,
        all_day=bool(row.all_day),
        tz=row.tz or "UTC",
    )


def _event_to_row(event: Event) -> EventRow:
    return EventRow(
        uid=event.uid,
        calendar_id=event.calendar_id,
        provider_event_id=event.provider_event_id,
        summary=event.summary,
        description=event.description,
        location=event.location,
        status=event.status,
        transparency=event.transparency,
        etag=event.etag,
        sequence=event.sequence,
        local_dirty=int(event.local_dirty),
        deleted_locally=int(event.deleted_locally),
        conflict_state=event.conflict_state,
        all_day=int(event.all_day),
        tz=event.tz,
        inserted_at=_utc_now(),
    )


class EventStore(QObject):
    events_changed = Signal(str, set)
    instances_changed = Signal(str, datetime, datetime)

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self._engine = engine

    def list_instances(
        self,
        start_utc: datetime,
        end_utc: datetime,
        calendar_ids: set[str] | None = None,
    ) -> list[EventInstanceRow]:
        with Session(self._engine) as s:
            q = s.query(EventInstanceRow).filter(
                EventInstanceRow.dtstart_utc < int(end_utc.timestamp()),
                EventInstanceRow.dtend_utc > int(start_utc.timestamp()),
            )
            if calendar_ids is not None:
                q = q.filter(EventInstanceRow.calendar_id.in_(calendar_ids))
            return q.all()

    def get_event(self, uid: str, calendar_id: str) -> Event | None:
        with Session(self._engine) as s:
            row = s.query(EventRow).filter_by(
                uid=uid, calendar_id=calendar_id
            ).first()
            if row is None:
                return None
            return _row_to_event(row)

    def queue_create(self, event: Event) -> None:
        with Session(self._engine) as s, s.begin():
            row = _event_to_row(event)
            row.local_dirty = True
            s.add(row)
        self.events_changed.emit(event.calendar_id, {event.uid})

    def queue_update(self, event: Event, prev_etag: str | None) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.query(EventRow).filter_by(
                uid=event.uid, calendar_id=event.calendar_id
            ).first()
            if row is not None:
                for field in (
                    "summary", "description", "location", "dtstart", "dtend",
                    "tz", "all_day", "rrule", "status", "transparency",
                ):
                    val = getattr(event, field, None)
                    if val is not None:
                        setattr(row, field, val)
                row.local_dirty = True
        self.events_changed.emit(event.calendar_id, {event.uid})

    def queue_delete(self, uid: str, calendar_id: str) -> None:
        with Session(self._engine) as s, s.begin():
            row = s.query(EventRow).filter_by(
                uid=uid, calendar_id=calendar_id
            ).first()
            if row is not None:
                row.deleted_locally = True
                row.local_dirty = True
        self.events_changed.emit(calendar_id, {uid})

    def apply_remote_changes(
        self,
        calendar_id: str,
        changes: list,
        new_cursor_json: str,
    ) -> int:
        count = 0
        with Session(self._engine) as s, s.begin():
            for change in changes:
                uid = getattr(change, "uid", "")
                if change.kind == "delete":
                    s.query(EventRow).filter_by(
                        uid=uid, calendar_id=calendar_id
                    ).delete()
                    count += 1
                elif change.kind == "upsert" and change.event is not None:
                    row = s.query(EventRow).filter_by(
                        uid=uid, calendar_id=calendar_id
                    ).first()
                    if row is None:
                        row = _event_to_row(change.event)
                        s.add(row)
                    else:
                        updated = _event_to_row(change.event)
                        for col in EventRow.__table__.columns.keys():
                            if col in ("uid", "calendar_id", "recurrence_id"):
                                continue
                            setattr(row, col, getattr(updated, col, None))
                        row.local_dirty = 0
                    count += 1
        self.events_changed.emit(calendar_id, {c.uid for c in changes if hasattr(c, "uid")})
        return count

    def list_accounts(self, enabled_only: bool = True) -> list:
        from lilical.models.account import Account
        with Session(self._engine) as s:
            q = s.query(Account)
            if enabled_only:
                q = q.filter(Account.enabled == 1)
            return q.all()

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list:
        from lilical.models.calendar import Calendar
        with Session(self._engine) as s:
            q = s.query(Calendar).filter(Calendar.account_id == account_id)
            if visible_only:
                q = q.filter(Calendar.is_visible == 1)
            return q.all()

    def list_pending_ops(self, account_id: str) -> list:
        from lilical.models.pending_op import PendingOpRow
        with Session(self._engine) as s:
            return s.query(PendingOpRow).filter(
                PendingOpRow.account_id == account_id
            ).order_by(PendingOpRow.created_at).all()

    def delete_pending_op(self, op_id: int) -> None:
        from lilical.models.pending_op import PendingOpRow
        with Session(self._engine) as s, s.begin():
            s.query(PendingOpRow).filter(PendingOpRow.id == op_id).delete()
