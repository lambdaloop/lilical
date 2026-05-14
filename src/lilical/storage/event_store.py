from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal
from sqlalchemy.orm import Session

from lilical.models.calendar import Calendar
from lilical.models.event import Event, EventInstanceRow, EventRow
from lilical.models.pending_op import PendingOpRow


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _json_dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj)


def _json_loads_tuple(s: str | None) -> tuple:
    if not s:
        return ()
    return tuple(json.loads(s))


def _row_to_event(row: EventRow) -> Event:
    return Event(
        uid=row.uid,
        calendar_id=row.calendar_id,
        provider_event_id=row.provider_event_id,
        dtstart=_parse_dt(row.dtstart),
        dtend=_parse_dt(row.dtend),
        tz=row.tz or "UTC",
        all_day=bool(row.all_day),
        summary=row.summary or "",
        description=row.description or "",
        location=row.location or "",
        url=row.url,
        rrule=row.rrule,
        exdates=_parse_dt_tuple(row.exdates),
        rdates=_parse_dt_tuple(row.rdates),
        attendees=_json_loads_tuple(row.attendees),
        categories=_json_loads_tuple(row.categories),
        color=row.color,
        status=row.status or "CONFIRMED",
        transparency=row.transparency or "OPAQUE",
        valarms=_json_loads_tuple(row.valarms),
        etag=row.etag,
        sequence=row.sequence or 0,
        last_modified=_parse_dt(row.last_modified),
        local_dirty=bool(row.local_dirty),
        deleted_locally=bool(row.deleted_locally),
        conflict_state=row.conflict_state,
    )


def _parse_dt_tuple(s: str | None) -> tuple[datetime, ...]:
    if not s:
        return ()
    raw = json.loads(s)
    return tuple(datetime.fromisoformat(x) for x in raw)


def _dt_tuple_to_json(dts: tuple[datetime, ...]) -> str | None:
    if not dts:
        return None
    return json.dumps([dt.isoformat() for dt in dts])


def _event_to_json(event: Event) -> str:
    d = dataclasses.asdict(event)
    d["dtstart"] = _to_iso(event.dtstart)
    d["dtend"] = _to_iso(event.dtend)
    d["recurrence_id"] = _to_iso(event.recurrence_id)
    d["last_modified"] = _to_iso(event.last_modified)
    d["exdates"] = [dt.isoformat() for dt in event.exdates]
    d["rdates"] = [dt.isoformat() for dt in event.rdates]
    d["attendees"] = list(event.attendees)
    d["categories"] = list(event.categories)
    d["valarms"] = list(event.valarms)
    return json.dumps(d)


def _event_to_row(event: Event) -> EventRow:
    return EventRow(
        uid=event.uid,
        calendar_id=event.calendar_id,
        provider_event_id=event.provider_event_id,
        dtstart=event.dtstart.isoformat() if event.dtstart else "",
        dtend=event.dtend.isoformat() if event.dtend else "",
        tz=event.tz,
        all_day=int(event.all_day),
        summary=event.summary,
        description=event.description,
        location=event.location,
        url=event.url,
        rrule=event.rrule,
        exdates=_dt_tuple_to_json(event.exdates),
        rdates=_dt_tuple_to_json(event.rdates),
        attendees=_json_dumps(list(event.attendees)) or "",
        categories=_json_dumps(list(event.categories)) or "",
        color=event.color,
        status=event.status,
        transparency=event.transparency,
        valarms=_json_dumps(list(event.valarms)) or "",
        etag=event.etag,
        sequence=event.sequence,
        last_modified=_to_iso(event.last_modified),
        local_dirty=int(event.local_dirty),
        deleted_locally=int(event.deleted_locally),
        conflict_state=event.conflict_state,
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

    def _account_id_for_calendar(self, calendar_id: str) -> str | None:
        cal = (
            Session(self._engine)
            .query(Calendar)
            .filter(Calendar.id == calendar_id)
            .first()
        )
        return cal.account_id if cal else None

    def queue_create(self, event: Event) -> None:
        account_id = self._account_id_for_calendar(event.calendar_id)
        with Session(self._engine) as s, s.begin():
            row = _event_to_row(event)
            row.local_dirty = True
            s.add(row)
            if account_id:
                s.add(PendingOpRow(
                    account_id=account_id,
                    calendar_id=event.calendar_id,
                    uid=event.uid,
                    op="create",
                    payload=_event_to_json(event),
                    if_match=None,
                    created_at=_utc_now(),
                ))
        self.events_changed.emit(event.calendar_id, {event.uid})

    def queue_update(self, event: Event, prev_etag: str | None) -> None:
        account_id = self._account_id_for_calendar(event.calendar_id)
        with Session(self._engine) as s, s.begin():
            row = s.query(EventRow).filter_by(
                uid=event.uid, calendar_id=event.calendar_id
            ).first()
            if row is not None:
                updated = _event_to_row(event)
                _skip = {"uid", "calendar_id", "recurrence_id", "inserted_at"}
                for col_name in EventRow.__table__.columns.keys():  # noqa: SIM118
                    if col_name in _skip:
                        continue
                    setattr(row, col_name, getattr(updated, col_name, None))
                row.local_dirty = True
                if account_id:
                    s.add(PendingOpRow(
                        account_id=account_id,
                        calendar_id=event.calendar_id,
                        uid=event.uid,
                        op="update",
                        payload=_event_to_json(event),
                        if_match=prev_etag,
                        created_at=_utc_now(),
                    ))
        self.events_changed.emit(event.calendar_id, {event.uid})

    def queue_delete(self, uid: str, calendar_id: str) -> None:
        account_id = self._account_id_for_calendar(calendar_id)
        with Session(self._engine) as s, s.begin():
            row = s.query(EventRow).filter_by(
                uid=uid, calendar_id=calendar_id
            ).first()
            if row is not None:
                row.deleted_locally = True
                row.local_dirty = True
                if account_id:
                    s.add(PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=uid,
                        op="delete",
                        payload="{}",
                        if_match=row.etag,
                        created_at=_utc_now(),
                    ))
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
                    local_event = dataclasses.replace(
                        change.event, calendar_id=calendar_id
                    )
                    row = s.query(EventRow).filter_by(
                        uid=uid, calendar_id=calendar_id
                    ).first()
                    if row is None:
                        row = _event_to_row(local_event)
                        s.add(row)
                    else:
                        updated = _event_to_row(local_event)
                        _skip = {"uid", "calendar_id", "recurrence_id"}
                        for col_name in EventRow.__table__.columns.keys():  # noqa: SIM118
                            if col_name in _skip:
                                continue
                            setattr(row, col_name, getattr(updated, col_name, None))
                        row.local_dirty = 0
                    count += 1
            if new_cursor_json:
                s.query(Calendar).filter(
                    Calendar.id == calendar_id
                ).update({"sync_cursor": new_cursor_json})
        changed_uids = {c.uid for c in changes if hasattr(c, "uid")}
        self.events_changed.emit(calendar_id, changed_uids)
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
