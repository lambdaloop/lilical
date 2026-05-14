from __future__ import annotations

from datetime import datetime
from sqlalchemy.orm import Session

from PySide6.QtCore import QObject, Signal

from lilical.models.event import EventRow, EventInstanceRow, Event


class EventStore(QObject):
    events_changed = Signal(str, set)
    instances_changed = Signal(str, datetime, datetime)

    def __init__(self, engine) -> None:
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

    def queue_create(self, event: Event) -> None:
        with Session(self._engine) as s, s.begin():
            row = EventRow(
                uid=event.uid,
                calendar_id=event.calendar_id,
            )
            s.add(row)
        self.events_changed.emit(event.calendar_id, {event.uid})

    def get_event(self, uid: str, calendar_id: str) -> Event | None:
        with Session(self._engine) as s:
            row = s.query(EventRow).filter_by(uid=uid, calendar_id=calendar_id).first()
            if row is None:
                return None
            return Event(uid=row.uid, calendar_id=row.calendar_id)

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
