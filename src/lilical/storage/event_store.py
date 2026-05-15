from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
from datetime import datetime, timedelta, timezone
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


def _json_loads_tuple(s: str | None) -> tuple[Any, ...]:
    if not s:
        return ()
    return tuple(json.loads(s))


def _row_to_event(row: EventRow) -> Event:
    return Event(
        uid=row.uid,
        calendar_id=row.calendar_id,
        recurrence_id=_parse_dt(row.recurrence_id) if row.recurrence_id else None,
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
        self_response=row.self_response,
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
        recurrence_id=event.recurrence_id.isoformat() if event.recurrence_id else "",
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
        self_response=event.self_response,
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
    local_events_changed = Signal()  # fired after any locally-originated mutation

    _instances_window_years = 1

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self._engine = engine
        self._write_lock = threading.RLock()

    @contextlib.contextmanager
    def _write_session(self):
        with self._write_lock:
            with Session(self._engine) as s, s.begin():
                yield s

    def rebuild_all_instances(self) -> None:
        now = datetime.now(timezone.utc)
        window_start = now.replace(year=now.year - self._instances_window_years)
        window_end = now.replace(year=now.year + self._instances_window_years)
        with self._write_session() as s:
            s.query(EventInstanceRow).delete()
            for row in s.query(EventRow).all():
                event = _row_to_event(row)
                self._rebuild_instances_for(s, event, window_start, window_end)

    @staticmethod
    def _ensure_aware_dt(val) -> datetime:
        from datetime import date as _date_cls
        from datetime import time

        if isinstance(val, _date_cls) and not isinstance(val, datetime):
            return datetime.combine(val, time.min, tzinfo=timezone.utc)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val
        return val

    def _rebuild_instances_for(
        self,
        session,
        event: Event,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> None:
        if not event.dtstart:
            return
        if window_start is None or window_end is None:
            now = datetime.now(timezone.utc)
            window_start = now.replace(year=now.year - self._instances_window_years)
            window_end = now.replace(year=now.year + self._instances_window_years)

        # Override rows belong to the master's instance set; delegate to master.
        if event.recurrence_id is not None:
            master_row = (
                session.query(EventRow)
                .filter_by(
                    uid=event.uid, calendar_id=event.calendar_id, recurrence_id=""
                )
                .first()
            )
            if master_row is not None:
                master_event = _row_to_event(master_row)
                self._rebuild_instances_for(
                    session, master_event, window_start, window_end
                )
            return

        session.query(EventInstanceRow).filter(
            EventInstanceRow.uid == event.uid,
            EventInstanceRow.calendar_id == event.calendar_id,
        ).delete()
        if event.rrule:
            from lilical.recurrence.expander import RecurrenceExpander

            # Use the current session to fetch overrides so we don't open a
            # nested session inside this transaction.
            override_rows = (
                session.query(EventRow)
                .filter(
                    EventRow.uid == event.uid,
                    EventRow.calendar_id == event.calendar_id,
                    EventRow.recurrence_id != "",
                    EventRow.deleted_locally == 0,
                )
                .all()
            )
            override_events = [_row_to_event(r) for r in override_rows]
            expander = RecurrenceExpander(self)
            for occ in expander.expand_for_storage(
                event, window_start, window_end, overrides=override_events
            ):
                ds = self._ensure_aware_dt(occ["dtstart"])
                de = self._ensure_aware_dt(occ["dtend"])
                session.add(
                    EventInstanceRow(
                        uid=occ["uid"],
                        calendar_id=occ["calendar_id"],
                        dtstart_utc=int(ds.timestamp()),
                        dtend_utc=int(de.timestamp()),
                        dtstart_local=ds.isoformat(),
                        dtend_local=de.isoformat(),
                        all_day=int(occ["all_day"]),
                        is_override=int(occ.get("is_override", False)),
                        recurrence_id=occ.get("recurrence_id") or "",
                    )
                )
            return
        dtstart = self._ensure_aware_dt(event.dtstart)
        dtend = self._ensure_aware_dt(event.dtend or event.dtstart)
        session.add(
            EventInstanceRow(
                uid=event.uid,
                calendar_id=event.calendar_id,
                dtstart_utc=int(dtstart.timestamp()),
                dtend_utc=int(dtend.timestamp()),
                dtstart_local=dtstart.isoformat(),
                dtend_local=dtend.isoformat(),
                all_day=int(event.all_day),
                is_override=0,
                recurrence_id="",
            )
        )

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
            # Prefer the master row (recurrence_id=""); fall back to any row.
            row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if row is None:
                row = (
                    s.query(EventRow)
                    .filter_by(uid=uid, calendar_id=calendar_id)
                    .first()
                )
            if row is None:
                return None
            return _row_to_event(row)

    def get_event_for_instance(self, inst: "EventInstanceRow") -> "Event | None":
        """Return the Event for a specific instance row.

        For override instances (recurrence_id != ""), returns the override Event
        so the chip displays the modified title/time. Falls back to the master.
        """
        if inst.recurrence_id:
            with Session(self._engine) as s:
                row = (
                    s.query(EventRow)
                    .filter_by(
                        uid=inst.uid,
                        calendar_id=inst.calendar_id,
                        recurrence_id=inst.recurrence_id,
                    )
                    .first()
                )
                if row is not None:
                    return _row_to_event(row)
        return self.get_event(inst.uid, inst.calendar_id)

    def events_for_instances(self, instances: list["EventInstanceRow"]) -> "dict[int, Event]":
        """Return {id(inst): Event} for a list of instances in one DB roundtrip.

        Prefers override rows for overridden instances; falls back to the master.
        """
        if not instances:
            return {}
        uids_by_cal: dict[str, set[str]] = {}
        for inst in instances:
            uids_by_cal.setdefault(inst.calendar_id, set()).add(inst.uid)

        by_key: dict[tuple[str, str, str], "EventRow"] = {}
        with Session(self._engine) as s:
            for cid, uids in uids_by_cal.items():
                for r in (
                    s.query(EventRow)
                    .filter(EventRow.calendar_id == cid, EventRow.uid.in_(uids))
                    .all()
                ):
                    by_key[(r.uid, r.calendar_id, r.recurrence_id or "")] = r

        out: dict[int, Event] = {}
        for inst in instances:
            rid = inst.recurrence_id or ""
            row = by_key.get((inst.uid, inst.calendar_id, rid)) if rid else None
            if row is None:
                row = by_key.get((inst.uid, inst.calendar_id, ""))
            if row is not None:
                out[id(inst)] = _row_to_event(row)
        return out

    def get_override_events(self, uid: str, calendar_id: str) -> list[Event]:
        """Return all non-deleted override EventRows for a recurring series."""
        with Session(self._engine) as s:
            rows = (
                s.query(EventRow)
                .filter(
                    EventRow.uid == uid,
                    EventRow.calendar_id == calendar_id,
                    EventRow.recurrence_id != "",
                    EventRow.deleted_locally == 0,
                )
                .all()
            )
            return [_row_to_event(r) for r in rows]

    def _account_id_for_calendar(self, calendar_id: str) -> str | None:
        with Session(self._engine) as s:
            cal = s.query(Calendar).filter(Calendar.id == calendar_id).first()
            return cal.account_id if cal else None

    def queue_create(self, event: Event) -> None:
        account_id = self._account_id_for_calendar(event.calendar_id)
        with self._write_session() as s:
            row = _event_to_row(event)
            row.local_dirty = True
            s.add(row)
            self._rebuild_instances_for(s, event)
            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=event.calendar_id,
                        uid=event.uid,
                        op="create",
                        payload=_event_to_json(event),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )
        self.events_changed.emit(event.calendar_id, {event.uid})
        self.local_events_changed.emit()

    def queue_update(self, event: Event, prev_etag: str | None) -> None:
        account_id = self._account_id_for_calendar(event.calendar_id)
        recurrence_id_str = (
            event.recurrence_id.isoformat() if event.recurrence_id else ""
        )
        with self._write_session() as s:
            row = (
                s.query(EventRow)
                .filter_by(
                    uid=event.uid,
                    calendar_id=event.calendar_id,
                    recurrence_id=recurrence_id_str,
                )
                .first()
            )
            if row is not None:
                updated = _event_to_row(event)
                _skip = {"uid", "calendar_id", "recurrence_id", "inserted_at"}
                for col_name in EventRow.__table__.columns.keys():  # noqa: SIM118
                    if col_name in _skip:
                        continue
                    setattr(row, col_name, getattr(updated, col_name, None))
                row.local_dirty = True
                self._rebuild_instances_for(s, event)
                if account_id:
                    s.add(
                        PendingOpRow(
                            account_id=account_id,
                            calendar_id=event.calendar_id,
                            uid=event.uid,
                            op="update",
                            payload=_event_to_json(event),
                            if_match=prev_etag,
                            created_at=_utc_now(),
                        )
                    )
        self.events_changed.emit(event.calendar_id, {event.uid})
        self.local_events_changed.emit()

    def queue_delete(self, uid: str, calendar_id: str) -> None:
        account_id = self._account_id_for_calendar(calendar_id)
        with self._write_session() as s:
            row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if row is not None:
                row.deleted_locally = True
                row.local_dirty = True
                s.query(EventInstanceRow).filter_by(
                    uid=uid, calendar_id=calendar_id
                ).delete()
                if account_id:
                    s.add(
                        PendingOpRow(
                            account_id=account_id,
                            calendar_id=calendar_id,
                            uid=uid,
                            op="delete",
                            payload="{}",
                            if_match=row.etag,
                            created_at=_utc_now(),
                        )
                    )
        self.events_changed.emit(calendar_id, {uid})
        self.local_events_changed.emit()

    def queue_move(
        self,
        uid: str,
        old_calendar_id: str,
        new_calendar_id: str,
        moved_event: Event,
    ) -> str:
        """Move an event from one calendar to another.

        Creates a new event row in the target calendar with a fresh uid and a
        pending create op. Marks the source event as deleted with a delete op.

        Returns the new event uid.
        """
        import uuid as _uuid

        account_id = self._account_id_for_calendar(old_calendar_id)
        new_uid = str(_uuid.uuid4())
        with self._write_session() as s:
            # Mark the old event as deleted
            old_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=old_calendar_id, recurrence_id="")
                .first()
            )
            if old_row is not None:
                old_row.deleted_locally = True
                old_row.local_dirty = True
                s.query(EventInstanceRow).filter_by(
                    uid=uid, calendar_id=old_calendar_id
                ).delete()
                if account_id:
                    s.add(
                        PendingOpRow(
                            account_id=account_id,
                            calendar_id=old_calendar_id,
                            uid=uid,
                            op="delete",
                            payload="{}",
                            if_match=old_row.etag,
                            created_at=_utc_now(),
                        )
                    )

            # Create the event in the new calendar
            moved = dataclasses.replace(
                moved_event,
                uid=new_uid,
                calendar_id=new_calendar_id,
                provider_event_id=None,
                etag=None,
                sequence=0,
                local_dirty=True,
            )
            new_row = _event_to_row(moved)
            new_row.local_dirty = True
            s.add(new_row)
            self._rebuild_instances_for(s, moved)

            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=new_calendar_id,
                        uid=new_uid,
                        op="create",
                        payload=_event_to_json(moved),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )

        self.events_changed.emit(old_calendar_id, {uid})
        self.events_changed.emit(new_calendar_id, {new_uid})
        self.local_events_changed.emit()
        return new_uid

    def queue_update_instance(
        self,
        uid: str,
        calendar_id: str,
        recurrence_id_dt: datetime,
        edited: Event,
    ) -> None:
        """Update a single instance of a recurring event."""
        account_id = self._account_id_for_calendar(calendar_id)
        recurrence_id_str = recurrence_id_dt.isoformat()
        with self._write_session() as s:
            row = (
                s.query(EventRow)
                .filter_by(
                    uid=uid, calendar_id=calendar_id, recurrence_id=recurrence_id_str
                )
                .first()
            )
            override = dataclasses.replace(
                edited,
                uid=uid,
                calendar_id=calendar_id,
                recurrence_id=recurrence_id_dt,
                rrule=None,
                local_dirty=True,
            )
            if row is None:
                s.add(_event_to_row(override))
            else:
                updated = _event_to_row(override)
                _skip = {"uid", "calendar_id", "recurrence_id", "inserted_at"}
                for col_name in EventRow.__table__.columns.keys():  # noqa: SIM118
                    if col_name in _skip:
                        continue
                    setattr(row, col_name, getattr(updated, col_name, None))
                row.local_dirty = True
            # Rebuild master's instances (expander will include this override).
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if master_row is not None:
                self._rebuild_instances_for(s, _row_to_event(master_row))
            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=uid,
                        op="update_instance",
                        payload=_event_to_json(override),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )
        self.events_changed.emit(calendar_id, {uid})
        self.local_events_changed.emit()

    def queue_delete_instance(
        self,
        uid: str,
        calendar_id: str,
        recurrence_id_dt: datetime,
    ) -> None:
        """Delete a single occurrence of a recurring event."""
        account_id = self._account_id_for_calendar(calendar_id)
        recurrence_id_str = recurrence_id_dt.isoformat()
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if master_row is not None:
                master_event = _row_to_event(master_row)
                new_exdates = master_event.exdates + (recurrence_id_dt,)
                master_row.exdates = _dt_tuple_to_json(new_exdates)
                master_row.local_dirty = True
                updated_master = dataclasses.replace(master_event, exdates=new_exdates)
                self._rebuild_instances_for(s, updated_master)
            # If there's an override row at this recurrence_id, mark it deleted too.
            override_row = (
                s.query(EventRow)
                .filter_by(
                    uid=uid, calendar_id=calendar_id, recurrence_id=recurrence_id_str
                )
                .first()
            )
            if override_row is not None:
                override_row.deleted_locally = True
                override_row.local_dirty = True
            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=uid,
                        op="delete_instance",
                        payload=json.dumps({"recurrence_id": recurrence_id_str}),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )
        self.events_changed.emit(calendar_id, {uid})
        self.local_events_changed.emit()

    def apply_remote_changes(
        self,
        calendar_id: str,
        changes: list[Any],
        new_cursor_json: str,
    ) -> int:
        count = 0
        # Events whose instances need rebuilding after the main transaction commits.
        # Keyed by canonical master (uid, calendar_id) to avoid redundant rebuilds
        # when both a master and its override(s) arrive in the same batch.
        masters_to_rebuild: dict[tuple[str, str], Event] = {}

        with self._write_session() as s:
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
                    recurrence_id_str = (
                        local_event.recurrence_id.isoformat()
                        if local_event.recurrence_id
                        else ""
                    )
                    row = (
                        s.query(EventRow)
                        .filter_by(
                            uid=uid,
                            calendar_id=calendar_id,
                            recurrence_id=recurrence_id_str,
                        )
                        .first()
                    )
                    if row is None and local_event.provider_event_id:
                        # Stale local-UUID uid from before mark_synced rewrote
                        # to canonical. Match on (calendar_id, provider_event_id)
                        # and adopt the canonical uid.
                        row = (
                            s.query(EventRow)
                            .filter_by(
                                calendar_id=calendar_id,
                                provider_event_id=local_event.provider_event_id,
                                recurrence_id=recurrence_id_str,
                            )
                            .first()
                        )
                        if row is not None and row.uid != uid:
                            old_uid = row.uid
                            row.uid = uid
                            s.query(EventRow).filter_by(
                                uid=old_uid, calendar_id=calendar_id
                            ).update({"uid": uid})
                            s.query(EventInstanceRow).filter_by(
                                uid=old_uid, calendar_id=calendar_id
                            ).update({"uid": uid})
                            s.query(PendingOpRow).filter_by(
                                uid=old_uid, calendar_id=calendar_id
                            ).update({"uid": uid})
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
                    # Defer instance rebuilds to after this transaction commits so
                    # the write lock is held only for the fast EventRow upserts and
                    # cursor update, not the expensive iCal expansion.
                    masters_to_rebuild[(uid, calendar_id)] = local_event
                    count += 1
            if new_cursor_json:
                s.query(Calendar).filter(Calendar.id == calendar_id).update(
                    {"sync_cursor": new_cursor_json}
                )
        # EventRows are now committed. Rebuild event_instances one master at a
        # time so the write lock is released between expansions — GUI writes can
        # interleave between masters.
        for event in masters_to_rebuild.values():
            with self._write_session() as s:
                self._rebuild_instances_for(s, event)
        changed_uids = {c.uid for c in changes if hasattr(c, "uid")}
        self.events_changed.emit(calendar_id, changed_uids)
        return count

    def list_events_in_range(
        self,
        start_utc: datetime,
        end_utc: datetime,
        calendar_ids: set[str] | None = None,
    ) -> list[Event]:
        with Session(self._engine) as s:
            q = s.query(EventRow).filter(
                EventRow.dtstart < end_utc.isoformat(),
                EventRow.dtend > start_utc.isoformat(),
            )
            if calendar_ids is not None:
                q = q.filter(EventRow.calendar_id.in_(calendar_ids))
            return [_row_to_event(r) for r in q.all()]

    def get_account(self, account_id: str):
        from lilical.models.account import Account

        with Session(self._engine) as s:
            return s.query(Account).filter(Account.id == account_id).first()

    def list_accounts(self, enabled_only: bool = True) -> list[Any]:
        from lilical.models.account import Account

        with Session(self._engine) as s:
            q = s.query(Account)
            if enabled_only:
                q = q.filter(Account.enabled == 1)
            return q.all()

    def update_account(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        identity: str | None = None,
        server_url: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        from lilical.models.account import Account

        with self._write_session() as s:
            acc = s.query(Account).filter(Account.id == account_id).first()
            if acc is None:
                return
            if display_name is not None:
                acc.display_name = display_name
            if identity is not None:
                acc.identity = identity
            if server_url is not None:
                acc.server_url = server_url
            if enabled is not None:
                acc.enabled = 1 if enabled else 0

    def delete_account(self, account_id: str) -> None:
        from lilical.models.account import Account
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            cal_ids = [
                cid
                for (cid,) in s.query(Calendar.id)
                .filter(Calendar.account_id == account_id)
                .all()
            ]
            if cal_ids:
                s.query(EventInstanceRow).filter(
                    EventInstanceRow.calendar_id.in_(cal_ids)
                ).delete(synchronize_session=False)
                s.query(EventRow).filter(EventRow.calendar_id.in_(cal_ids)).delete(
                    synchronize_session=False
                )
            s.query(PendingOpRow).filter(PendingOpRow.account_id == account_id).delete(
                synchronize_session=False
            )
            s.query(Calendar).filter(Calendar.account_id == account_id).delete(
                synchronize_session=False
            )
            s.query(Account).filter(Account.id == account_id).delete(
                synchronize_session=False
            )

    def set_calendar_visibility(self, calendar_id: str, is_visible: bool) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            s.query(Calendar).filter(Calendar.id == calendar_id).update(
                {"is_visible": 1 if is_visible else 0}
            )

    def get_calendar(self, calendar_id: str):
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            return s.query(Calendar).filter(Calendar.id == calendar_id).first()

    def set_calendar_color(self, calendar_id: str, color: str) -> None:
        """User-set color. Fires events_changed so views re-render with the new tint."""
        from lilical.models.calendar import Calendar

        cal_account_id = None
        with self._write_session() as s:
            row = s.query(Calendar).filter(Calendar.id == calendar_id).first()
            if row is None:
                return
            row.color = color
            cal_account_id = row.account_id
        # No targeted "calendar metadata changed" signal exists yet; piggy-back
        # on events_changed with an empty UID set to nudge any view watching
        # this calendar to re-paint.
        self.events_changed.emit(calendar_id, set())
        _ = cal_account_id  # reserved for a future account-level signal

    def create_account(
        self,
        account_id: str,
        kind: str,
        display_name: str,
        identity: str,
        server_url: str | None,
        calendar_id: str,
        calendar_display_name: str,
    ) -> None:
        from lilical.models.account import Account
        from lilical.models.calendar import Calendar

        now = datetime.now(timezone.utc).isoformat()
        with self._write_session() as s:
            s.add(
                Account(
                    id=account_id,
                    kind=kind,
                    display_name=display_name,
                    identity=identity,
                    server_url=server_url,
                    secret_ref=account_id,
                    created_at=now,
                    enabled=1,
                )
            )
            s.add(
                Calendar(
                    id=calendar_id,
                    account_id=account_id,
                    provider_id="default",
                    display_name=calendar_display_name,
                    color="#5e9fff",
                    is_primary=1,
                    is_visible=1,
                    access_role="owner",
                )
            )

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list[Any]:
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            q = s.query(Calendar).filter(Calendar.account_id == account_id)
            if visible_only:
                q = q.filter(Calendar.is_visible == 1)
            return q.all()

    def visible_calendar_ids(self) -> set[str]:
        """IDs of every visible calendar across every enabled account.

        Returned as a `set` (never None) so views can pass it directly as the
        `calendar_ids` filter to `list_instances`: a `set()` means "no visible
        calendars, render nothing" — explicitly distinct from `None` ("no
        filter, render everything").
        """
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            rows = s.query(Calendar.id).filter(Calendar.is_visible == 1).all()
            return {row[0] for row in rows}

    # 12-colour fallback palette for calendars the backend doesn't tint.
    # Hash of provider_id picks the slot, so order is stable across syncs.
    _FALLBACK_PALETTE: tuple[str, ...] = (
        "#5e9fff",  # blue
        "#5cc97a",  # green
        "#e25c5c",  # red
        "#f59e0b",  # orange
        "#a855f7",  # purple
        "#ec4899",  # pink
        "#14b8a6",  # teal
        "#eab308",  # yellow
        "#06b6d4",  # cyan
        "#84cc16",  # lime
        "#6366f1",  # indigo
        "#f43f5e",  # rose
    )
    # The colour that historically meant "no color chosen" — we treat it as
    # unset so a backend-provided color can replace it on first sync.
    _LEGACY_DEFAULT_COLOR = "#5e9fff"

    @classmethod
    def _fallback_color(cls, provider_id: str) -> str:
        # hash() is salted per-process — use a stable hash for deterministic
        # palette assignment across restarts.
        import hashlib

        idx = int(hashlib.sha1(provider_id.encode("utf-8")).hexdigest(), 16) % len(
            cls._FALLBACK_PALETTE
        )
        return cls._FALLBACK_PALETTE[idx]

    def upsert_calendars(
        self, account_id: str, calendars: list[dict[str, Any]]
    ) -> None:
        """Reconcile local calendar rows with the backend's calendar list.

        Inserts new calendars, updates display_name on existing ones, and removes
        the bootstrap placeholder row (provider_id="default") when the backend
        returns real IDs that don't include "default".

        Colours: if the backend provided a colour, use it for new calendars and
        for existing calendars whose stored colour is still the legacy default
        (treated as "unset" so a user override won't be clobbered). If the
        backend didn't provide a colour, fall back to the curated palette
        deterministically picked by provider_id hash.
        """
        import uuid

        from lilical.models.calendar import Calendar

        remote_pids = {c["provider_id"] for c in calendars if c.get("provider_id")}
        with self._write_session() as s:
            existing = s.query(Calendar).filter(Calendar.account_id == account_id).all()
            existing_by_pid = {c.provider_id: c for c in existing}

            for cal in existing:
                if cal.provider_id == "default" and "default" not in remote_pids:
                    s.delete(cal)

            for remote in calendars:
                pid = remote.get("provider_id")
                if not pid:
                    continue
                name = remote.get("display_name") or pid
                remote_color = remote.get("color")
                if pid in existing_by_pid:
                    cal = existing_by_pid[pid]
                    if cal.display_name != name:
                        cal.display_name = name
                    # Only overwrite stored color if the local value is the
                    # legacy default sentinel — preserves user overrides.
                    if cal.color in (None, "", self._LEGACY_DEFAULT_COLOR):
                        cal.color = remote_color or self._fallback_color(pid)
                else:
                    s.add(
                        Calendar(
                            id=str(uuid.uuid4()),
                            account_id=account_id,
                            provider_id=pid,
                            display_name=name,
                            color=remote_color or self._fallback_color(pid),
                            is_primary=0,
                            is_visible=1,
                            access_role="owner",
                        )
                    )

    def list_pending_ops(self, account_id: str) -> list[Any]:
        from lilical.models.pending_op import PendingOpRow

        with Session(self._engine) as s:
            return (
                s.query(PendingOpRow)
                .filter(PendingOpRow.account_id == account_id)
                .order_by(PendingOpRow.created_at)
                .all()
            )

    def delete_pending_op(self, op_id: int) -> None:
        from lilical.models.pending_op import PendingOpRow

        with self._write_session() as s:
            s.query(PendingOpRow).filter(PendingOpRow.id == op_id).delete()

    def get_pending_op(self, op_id: int):
        from lilical.models.pending_op import PendingOpRow

        with Session(self._engine) as s:
            return s.query(PendingOpRow).filter(PendingOpRow.id == op_id).first()

    def remove_event(self, uid: str, calendar_id: str) -> None:
        with self._write_session() as s:
            s.query(EventRow).filter_by(uid=uid, calendar_id=calendar_id).delete()
            s.query(EventInstanceRow).filter_by(
                uid=uid, calendar_id=calendar_id
            ).delete()

    def mark_synced(
        self,
        local_uid: str,
        calendar_id: str,
        *,
        canonical_uid: str | None,
        provider_event_id: str | None,
        etag: str | None,
        sequence: int,
    ) -> None:
        """Mark a locally-created event as synced without re-queueing an update.

        When canonical_uid differs from local_uid (e.g. Graph returns its own
        id), cascade the uid rewrite to override rows, expanded instances and
        any pending ops still queued for this event.
        """
        with self._write_session() as s:
            row = (
                s.query(EventRow)
                .filter_by(uid=local_uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if row is None:
                return
            if provider_event_id is not None:
                row.provider_event_id = provider_event_id
            if etag is not None:
                row.etag = etag
            row.sequence = sequence
            row.local_dirty = 0
            if canonical_uid and canonical_uid != local_uid:
                row.uid = canonical_uid
                s.query(EventRow).filter_by(
                    uid=local_uid, calendar_id=calendar_id
                ).update({"uid": canonical_uid})
                s.query(EventInstanceRow).filter_by(
                    uid=local_uid, calendar_id=calendar_id
                ).update({"uid": canonical_uid})
                s.query(PendingOpRow).filter_by(
                    uid=local_uid, calendar_id=calendar_id
                ).update({"uid": canonical_uid})

    def queue_split_series(
        self,
        uid: str,
        calendar_id: str,
        split_at_dt: datetime,
        edited_event_for_tail: Event,
    ) -> str:
        """Split a recurring series at split_at_dt.

        Appends UNTIL=<split_at_dt - 1 second> to the master's RRULE (converting
        COUNT to UNTIL when present), enqueues an update op for the master, then
        creates a new series tail starting at split_at_dt with a new UID.

        Returns the new tail event UID.
        """
        import re as _re
        import uuid

        account_id = self._account_id_for_calendar(calendar_id)
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if master_row is None:
                raise ValueError(f"No master event {uid} in calendar {calendar_id}")

            master_event = _row_to_event(master_row)

            # Compute UNTIL = one second before the split point (inclusive boundary)
            until_dt = split_at_dt - timedelta(seconds=1)
            until_str = until_dt.strftime("%Y%m%dT%H%M%SZ")

            # Update master RRULE: remove COUNT, set UNTIL
            rrule = master_event.rrule or ""
            rrule = _re.sub(r";?COUNT=\d+", "", rrule)
            rrule = _re.sub(r";?UNTIL=[^;]+", "", rrule)
            rrule = rrule.rstrip(";") + f";UNTIL={until_str}"

            master_row.rrule = rrule
            master_row.local_dirty = True

            # Rebuild instances for the truncated master
            updated_master = dataclasses.replace(master_event, rrule=rrule)
            self._rebuild_instances_for(s, updated_master)

            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=uid,
                        op="update",
                        payload=_event_to_json(updated_master),
                        if_match=master_event.etag,
                        created_at=_utc_now(),
                    )
                )

            # Build the tail event
            new_uid = str(uuid.uuid4())
            tail = dataclasses.replace(
                edited_event_for_tail,
                uid=new_uid,
                calendar_id=calendar_id,
                recurrence_id=None,
                provider_event_id=None,
                etag=None,
                sequence=0,
                dtstart=split_at_dt,
                local_dirty=True,
            )
            tail_row = _event_to_row(tail)
            tail_row.local_dirty = True
            s.add(tail_row)
            self._rebuild_instances_for(s, tail)

            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=new_uid,
                        op="create",
                        payload=_event_to_json(tail),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )

        self.events_changed.emit(calendar_id, {uid, new_uid})
        self.local_events_changed.emit()
        return new_uid

    def queue_truncate_series(
        self,
        uid: str,
        calendar_id: str,
        until_dt: datetime,
    ) -> None:
        """Truncate a recurring series so no occurrences exist at or after until_dt.

        Sets UNTIL=<until_dt - 1 second> on the master's RRULE, deletes override
        instances from until_dt onwards, and enqueues an update op.
        """
        import re as _re

        account_id = self._account_id_for_calendar(calendar_id)
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if master_row is None:
                raise ValueError(f"No master event {uid} in calendar {calendar_id}")

            master_event = _row_to_event(master_row)
            cut = until_dt - timedelta(seconds=1)
            until_str = cut.strftime("%Y%m%dT%H%M%SZ")

            rrule = master_event.rrule or ""
            rrule = _re.sub(r";?COUNT=\d+", "", rrule)
            rrule = _re.sub(r";?UNTIL=[^;]+", "", rrule)
            rrule = rrule.rstrip(";") + f";UNTIL={until_str}"

            master_row.rrule = rrule
            master_row.local_dirty = True

            # Remove override rows on or after the cutoff
            s.query(EventRow).filter(
                EventRow.uid == uid,
                EventRow.calendar_id == calendar_id,
                EventRow.recurrence_id != "",
                EventRow.recurrence_id >= until_dt.isoformat(),
            ).delete(synchronize_session=False)

            updated_master = dataclasses.replace(master_event, rrule=rrule)
            self._rebuild_instances_for(s, updated_master)

            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=calendar_id,
                        uid=uid,
                        op="update",
                        payload=_event_to_json(updated_master),
                        if_match=master_event.etag,
                        created_at=_utc_now(),
                    )
                )

        self.events_changed.emit(calendar_id, {uid})
        self.local_events_changed.emit()
