from __future__ import annotations

import contextlib
import dataclasses
import json
import threading
import zoneinfo
from datetime import datetime, time, timedelta, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal
from sqlalchemy import func
from sqlalchemy.orm import Session

from lilical.models.calendar import Calendar
from lilical.models.event import (
    Attendee,
    Event,
    EventCompletionRow,
    EventInstanceRow,
    EventRow,
    Organizer,
)
from lilical.models.pending_op import PendingOpRow
from lilical.recurrence.identity import MASTER_KEY, recurrence_key
from lilical.utils.timezone import local_iana_tz


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


def _attendee_from_json(d: Any) -> Attendee:
    if isinstance(d, dict):
        return Attendee(
            email=str(d.get("email", "")),
            display_name=d.get("display_name") or None,
            response=str(d.get("response", "NEEDS-ACTION")),
            is_organizer=bool(d.get("is_organizer", False)),
            is_self=bool(d.get("is_self", False)),
        )
    # Legacy string shape — strip mailto: and iCal params.
    raw = str(d)
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    return Attendee(email=raw)


def _organizer_from_json(d: Any) -> Organizer | None:
    if not d or not isinstance(d, dict):
        return None
    return Organizer(
        email=str(d.get("email", "")),
        display_name=d.get("display_name") or None,
        is_self=bool(d.get("is_self", False)),
    )


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
        # exdates is the *effective* set — server EXDATEs plus locally-deleted
        # occurrences the server has not confirmed yet. Every reader (expander,
        # serializers, backends) wants that union and nothing else.
        exdates=_merge_exdates(
            _parse_dt_tuple(row.exdates),
            _local_exdate_dts(row.local_exdates),
            bool(row.all_day),
        ),
        local_exdates=_local_exdate_dts(row.local_exdates),
        rdates=_parse_dt_tuple(row.rdates),
        attendees=tuple(
            _attendee_from_json(a)
            for a in (json.loads(row.attendees) if row.attendees else [])
        ),
        organizer=_organizer_from_json(
            json.loads(row.organizer) if row.organizer else None
        ),
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


def _exdate_key(dt: datetime, all_day: bool = False) -> int:
    """UTC epoch-minute key for deduping EXDATEs across tz/DST representations."""
    return recurrence_key(dt, all_day=all_day)


def _merge_exdates(
    base: tuple[datetime, ...],
    extra: tuple[datetime, ...],
    all_day: bool = False,
) -> tuple[datetime, ...]:
    """Union two EXDATE tuples, deduping by instant (minute granularity)."""
    seen = {_exdate_key(d, all_day) for d in base}
    merged = list(base)
    for d in extra:
        k = _exdate_key(d, all_day)
        if k not in seen:
            seen.add(k)
            merged.append(d)
    return tuple(merged)


def _subtract_exdates(
    base: tuple[datetime, ...], remove: tuple[datetime, ...], all_day: bool = False
) -> tuple[datetime, ...]:
    """base minus remove, compared by instant."""
    drop = {_exdate_key(d, all_day) for d in remove}
    return tuple(d for d in base if _exdate_key(d, all_day) not in drop)


# ── Local occurrence tombstones ───────────────────────────────────────────────
# A single-occurrence delete has to outlive the pending op that pushes it: the
# op is reaped as soon as it uploads, but the master upsert that arrives in the
# same sync tick carries no EXDATE on Google or Graph. Without a durable record
# the occurrence the user just deleted comes straight back. Entries are stored
# on the master row as JSON and retired by _reconcile_local_exdates once the
# server's own representation carries the information.

# Backstop so a tombstone can never hide an occurrence indefinitely.
_LOCAL_EXDATE_TTL_DAYS = 7


def _load_local_exdates(raw: str | None) -> list[dict[str, Any]]:
    """Parse the tombstone list, tolerating the plain-ISO-list legacy shape."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            if item.get("rid"):
                out.append(item)
        elif isinstance(item, str):
            out.append({"rid": item, "since": _utc_now(), "pushed": False})
    return out


def _dump_local_exdates(entries: list[dict[str, Any]]) -> str | None:
    return json.dumps(entries) if entries else None


def _entry_key(entry: dict[str, Any], all_day: bool = False) -> int:
    try:
        return recurrence_key(datetime.fromisoformat(entry["rid"]), all_day=all_day)
    except (ValueError, TypeError, KeyError):
        return MASTER_KEY


def _local_exdate_dts(raw: str | None) -> tuple[datetime, ...]:
    out: list[datetime] = []
    for e in _load_local_exdates(raw):
        with contextlib.suppress(ValueError, TypeError):
            out.append(datetime.fromisoformat(e["rid"]))
    return tuple(out)


# _pending_delete_instance_exdates used to guard a just-deleted occurrence
# against a remote master upsert, but only while the op sat in the queue. That
# window closed the instant the op uploaded, which is exactly when the occurrence
# came back. local_exdates covers the same window and outlives the op, so the
# pending-op lookup is gone.


def _match_row(query, recurrence_id_str: str, rkey: int):
    """Pick the master row, or the override row for a slot matched by instant."""
    if recurrence_id_str == "":
        return query.filter_by(recurrence_id="").first()
    return query.filter(
        EventRow.recurrence_id != "", EventRow.recurrence_key == rkey
    ).first()


def _event_to_json(event: Event) -> str:
    d = dataclasses.asdict(event)
    # Fix datetime fields (asdict preserves datetime objects, json.dumps won't).
    d["dtstart"] = _to_iso(event.dtstart)
    d["dtend"] = _to_iso(event.dtend)
    d["recurrence_id"] = _to_iso(event.recurrence_id)
    d["last_modified"] = _to_iso(event.last_modified)
    d["exdates"] = [dt.isoformat() for dt in event.exdates]
    d["local_exdates"] = [dt.isoformat() for dt in event.local_exdates]
    d["rdates"] = [dt.isoformat() for dt in event.rdates]
    # attendees / organizer are already dicts via dataclasses.asdict — keep them.
    d["categories"] = list(event.categories)
    d["valarms"] = list(event.valarms)
    return json.dumps(d)


def _event_to_row(event: Event) -> EventRow:
    return EventRow(
        uid=event.uid,
        calendar_id=event.calendar_id,
        recurrence_id=event.recurrence_id.isoformat() if event.recurrence_id else "",
        recurrence_key=recurrence_key(event.recurrence_id, all_day=event.all_day),
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
        # Event.exdates is the effective union, so split the local tombstones
        # back out — otherwise they'd be laundered into the server column and
        # could never be reconciled away.
        exdates=_dt_tuple_to_json(
            _subtract_exdates(event.exdates, event.local_exdates, event.all_day)
        ),
        local_exdates=_dump_local_exdates(
            [
                {"rid": d.isoformat(), "since": _utc_now(), "pushed": False}
                for d in event.local_exdates
            ]
        ),
        rdates=_dt_tuple_to_json(event.rdates),
        attendees=_json_dumps([dataclasses.asdict(a) for a in event.attendees]) or "",
        organizer=_json_dumps(
            dataclasses.asdict(event.organizer) if event.organizer else None
        ),
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
    cal_metadata_changed = Signal(str)  # calendar_id — color or visibility changed
    instance_completion_changed = Signal(str, str, int)  # calendar_id, uid, dtstart_utc

    _instances_window_years = 1

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self._engine = engine
        self._write_lock = threading.RLock()
        # Set by app.py after construction; allows UI and sync to access contacts.
        self.contacts: Any = None

    @contextlib.contextmanager
    def _write_session(self):
        with self._write_lock, Session(self._engine) as s, s.begin():
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

        if isinstance(val, _date_cls) and not isinstance(val, datetime):
            return datetime.combine(val, time.min, tzinfo=timezone.utc)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val
        return val

    @staticmethod
    def _anchor_all_day(dt: datetime) -> datetime:
        """Re-anchor an all-day datetime to local-zone midnight on the same
        wall-clock date.  Uses dt.date() directly so UTC-midnight-encoded
        events (Radicale/Baikal style) keep their intended calendar day
        instead of shifting to the previous evening for users west of UTC."""
        local_zone = zoneinfo.ZoneInfo(local_iana_tz())
        return datetime.combine(dt.date(), time.min, tzinfo=local_zone)

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
                if occ["all_day"]:
                    ds = self._anchor_all_day(ds)
                    de = self._anchor_all_day(de)
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
                        recurrence_key=recurrence_key(
                            _parse_dt(occ.get("recurrence_id") or None),
                            all_day=bool(occ["all_day"]),
                        ),
                    )
                )
            return
        dtstart = self._ensure_aware_dt(event.dtstart)
        dtend = self._ensure_aware_dt(event.dtend or event.dtstart)
        if event.all_day:
            dtstart = self._anchor_all_day(dtstart)
            dtend = self._anchor_all_day(dtend)
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

    def get_override_etag(
        self,
        uid: str,
        calendar_id: str,
        recurrence_id_dt: datetime,
        all_day: bool = False,
    ) -> str | None:
        """Return the stored etag of an override row, or None if absent.

        Used to send If-Match on per-occurrence writes so a concurrent server
        edit to that occurrence isn't silently clobbered. Unlike
        get_override_events this ignores deleted_locally, since delete_instance
        needs the etag of the row it just tombstoned.

        Matches on recurrence_key rather than the ISO string, so the caller's
        spelling of the instant doesn't have to equal the provider's.
        """
        with Session(self._engine) as s:
            row = self._find_override_row(
                s, uid, calendar_id, recurrence_id_dt, all_day
            )
            return row.etag if row else None

    @staticmethod
    def _find_override_row(
        session,
        uid: str,
        calendar_id: str,
        recurrence_id_dt: datetime,
        all_day: bool = False,
    ) -> "EventRow | None":
        """The override row for a slot, matched by instant not by ISO string."""
        return (
            session.query(EventRow)
            .filter_by(
                uid=uid,
                calendar_id=calendar_id,
                recurrence_key=recurrence_key(recurrence_id_dt, all_day=all_day),
            )
            .filter(EventRow.recurrence_id != "")
            .first()
        )

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
                        recurrence_key=inst.recurrence_key,
                    )
                    .filter(EventRow.recurrence_id != "")
                    .first()
                )
                if row is not None:
                    return _row_to_event(row)
        return self.get_event(inst.uid, inst.calendar_id)

    def events_for_instances(
        self, instances: list["EventInstanceRow"]
    ) -> "dict[int, Event]":
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

    def completion_for_instances(
        self, instances: "list[EventInstanceRow]"
    ) -> "frozenset[tuple[str, str, int]]":
        """Return (calendar_id, uid, dtstart_utc) triples marked completed."""
        if not instances:
            return frozenset()
        keys = [(i.calendar_id, i.uid, i.dtstart_utc) for i in instances]
        with Session(self._engine) as s:
            rows = (
                s.query(EventCompletionRow)
                .filter(
                    EventCompletionRow.calendar_id.in_({k[0] for k in keys}),
                    EventCompletionRow.dtstart_utc.in_({k[2] for k in keys}),
                )
                .all()
            )
        key_set = {(r.calendar_id, r.uid, r.dtstart_utc) for r in rows}
        return frozenset(k for k in keys if k in key_set)

    def set_completed(
        self, calendar_id: str, uid: str, dtstart_utc: int, completed: bool
    ) -> None:
        """Insert or delete the completion row for one occurrence."""
        with self._write_session() as s:
            row = (
                s.query(EventCompletionRow)
                .filter_by(calendar_id=calendar_id, uid=uid, dtstart_utc=dtstart_utc)
                .first()
            )
            if completed and row is None:
                s.add(
                    EventCompletionRow(
                        calendar_id=calendar_id,
                        uid=uid,
                        dtstart_utc=dtstart_utc,
                        completed_at=_utc_now(),
                    )
                )
            elif not completed and row is not None:
                s.delete(row)
        self.instance_completion_changed.emit(calendar_id, uid, dtstart_utc)

    def is_completed(self, calendar_id: str, uid: str, dtstart_utc: int) -> bool:
        """Single-instance lookup for the event details dialog."""
        with Session(self._engine) as s:
            return (
                s.query(EventCompletionRow)
                .filter_by(calendar_id=calendar_id, uid=uid, dtstart_utc=dtstart_utc)
                .first()
            ) is not None

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

    def queue_respond(self, uid: str, calendar_id: str, response: str) -> None:
        """Record a local RSVP response and enqueue it for sync to the backend."""
        import json as _json

        account_id = self._account_id_for_calendar(calendar_id)
        with self._write_session() as s:
            row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if row is not None:
                row.self_response = response
                row.local_dirty = True
                row.local_modified_at = _utc_now()
                if account_id:
                    s.add(
                        PendingOpRow(
                            account_id=account_id,
                            calendar_id=calendar_id,
                            uid=uid,
                            op="respond",
                            payload=_json.dumps({"response": response}),
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

        # The delete op belongs to the source calendar's account; the create op
        # belongs to the *target* calendar's account. For a cross-account move
        # these differ — using the old account for the create op makes the wrong
        # backend try to push the event and fail (e.g. CalDAV PUT to a Graph
        # calendar id -> 404).
        account_id = self._account_id_for_calendar(old_calendar_id)
        new_account_id = self._account_id_for_calendar(new_calendar_id)
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

            if new_account_id:
                s.add(
                    PendingOpRow(
                        account_id=new_account_id,
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

    def queue_copy(self, source_event: Event, target_calendar_id: str) -> str:
        """Copy an event into a different calendar, leaving the source intact.

        Attendees, organizer, and self_response are stripped so the target
        account does not re-send invites.  Reminders (valarms) are preserved.
        Returns the new event uid.
        """
        import uuid as _uuid

        new_uid = str(_uuid.uuid4())
        copy = dataclasses.replace(
            source_event,
            uid=new_uid,
            calendar_id=target_calendar_id,
            provider_event_id=None,
            etag=None,
            sequence=0,
            attendees=(),
            organizer=None,
            self_response=None,
            local_dirty=True,
        )
        account_id = self._account_id_for_calendar(target_calendar_id)
        with self._write_session() as s:
            new_row = _event_to_row(copy)
            new_row.local_dirty = True
            s.add(new_row)
            self._rebuild_instances_for(s, copy)
            if account_id:
                s.add(
                    PendingOpRow(
                        account_id=account_id,
                        calendar_id=target_calendar_id,
                        uid=new_uid,
                        op="create",
                        payload=_event_to_json(copy),
                        if_match=None,
                        created_at=_utc_now(),
                    )
                )
        self.events_changed.emit(target_calendar_id, {new_uid})
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
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            all_day = bool(master_row.all_day) if master_row is not None else False
            # Match by instant: the UI's fixed-offset spelling of this slot will
            # not equal the provider's, and an exact-string miss would insert a
            # duplicate override row instead of updating the existing one.
            row = self._find_override_row(
                s, uid, calendar_id, recurrence_id_dt, all_day
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
                # recurrence_id is skipped so the provider's own spelling of the
                # slot survives for outbound requests.
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
        """Delete a single occurrence of a recurring event.

        Records a durable local tombstone rather than only an EXDATE guarded by
        the pending op: the op is reaped the moment it uploads, and the master
        upsert arriving in the same sync tick carries no EXDATE on Google or
        Graph. _reconcile_local_exdates retires the tombstone once the server's
        own representation carries the deletion.
        """
        account_id = self._account_id_for_calendar(calendar_id)
        recurrence_id_str = recurrence_id_dt.isoformat()
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            all_day = bool(master_row.all_day) if master_row is not None else False
            # Tombstone an existing override BEFORE rebuilding. The rebuild only
            # skips overrides with deleted_locally set, so doing this afterwards
            # re-emits the instance and the occurrence never disappears.
            override_row = self._find_override_row(
                s, uid, calendar_id, recurrence_id_dt, all_day
            )
            if override_row is not None:
                override_row.deleted_locally = True
                override_row.local_dirty = True
            if master_row is not None:
                k = recurrence_key(recurrence_id_dt, all_day=all_day)
                entries = _load_local_exdates(master_row.local_exdates)
                if not any(_entry_key(e, all_day) == k for e in entries):
                    entries.append(
                        {
                            "rid": recurrence_id_str,
                            "since": _utc_now(),
                            "pushed": False,
                        }
                    )
                master_row.local_exdates = _dump_local_exdates(entries)
                # local_dirty is deliberately not set: no master update op is
                # enqueued for this, and the EXDATE must not be pushed as part
                # of the master (Google and Graph masters carry none).
                self._rebuild_instances_for(s, _row_to_event(master_row))
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

    @staticmethod
    def _reconcile_local_exdates(session, master_row: "EventRow") -> None:
        """Retire local occurrence tombstones the server now accounts for.

        A tombstone exists only to bridge the gap between "the user deleted this
        occurrence" and "the server's own representation shows it gone". Drop it
        as soon as the server carries that information itself:

        * the server master now has an EXDATE at the slot (CalDAV, and Graph via
          synthesis) — the server column already produces the hole;
        * a CANCELLED override sits at the slot (Google's cancelled instance,
          Graph's cancelled exception) — the expander already produces the hole;
        * an *active* override sits at the slot and our deletion has been pushed
          — the occurrence was re-created server-side and must become visible
          again. Gated on `pushed` so we don't release during the window between
          queueing the delete and uploading it, when the server still legitimately
          reports the occurrence as present.

        Plus a TTL backstop, so a tombstone can never hide an occurrence forever
        if none of the above ever arrives.
        """
        entries = _load_local_exdates(master_row.local_exdates)
        if not entries:
            return
        all_day = bool(master_row.all_day)
        server_keys = {
            _exdate_key(d, all_day) for d in _parse_dt_tuple(master_row.exdates)
        }
        override_status: dict[int, str] = {
            r.recurrence_key: (r.status or "CONFIRMED")
            for r in session.query(EventRow)
            .filter(
                EventRow.uid == master_row.uid,
                EventRow.calendar_id == master_row.calendar_id,
                EventRow.recurrence_id != "",
                EventRow.deleted_locally == 0,
            )
            .all()
        }
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOCAL_EXDATE_TTL_DAYS)

        kept: list[dict[str, Any]] = []
        for e in entries:
            k = _entry_key(e, all_day)
            if k in server_keys:
                continue
            status = override_status.get(k)
            if status == "CANCELLED":
                continue
            if status is not None and e.get("pushed"):
                continue
            since = _parse_dt(e.get("since"))
            if since is not None and since < cutoff:
                continue
            kept.append(e)
        if len(kept) != len(entries):
            master_row.local_exdates = _dump_local_exdates(kept)

    def mark_delete_instance_pushed(
        self, uid: str, calendar_id: str, recurrence_id_dt: datetime
    ) -> None:
        """Record that a single-occurrence delete reached the server.

        Only after this can a re-created occurrence release its tombstone; see
        _reconcile_local_exdates.
        """
        with self._write_session() as s:
            master_row = (
                s.query(EventRow)
                .filter_by(uid=uid, calendar_id=calendar_id, recurrence_id="")
                .first()
            )
            if master_row is None:
                return
            all_day = bool(master_row.all_day)
            k = recurrence_key(recurrence_id_dt, all_day=all_day)
            entries = _load_local_exdates(master_row.local_exdates)
            changed = False
            for e in entries:
                if _entry_key(e, all_day) == k and not e.get("pushed"):
                    e["pushed"] = True
                    changed = True
            if changed:
                master_row.local_exdates = _dump_local_exdates(entries)

    def apply_remote_changes(
        self,
        calendar_id: str,
        changes: list[Any],
        new_cursor_json: str,
        *,
        rebuild_batch_size: int = 1,
    ) -> int:
        count = 0
        # Events whose instances need rebuilding after the main transaction commits.
        # Keyed by canonical master (uid, calendar_id) to avoid redundant rebuilds
        # when both a master and its override(s) arrive in the same batch.
        masters_to_rebuild: dict[tuple[str, str], Event] = {}
        # Masters whose local tombstones should be re-checked against the batch.
        masters_touched: set[tuple[str, str]] = set()

        with self._write_session() as s:
            for change in changes:
                uid = getattr(change, "uid", "")
                if change.kind == "delete":
                    s.query(EventRow).filter_by(
                        uid=uid, calendar_id=calendar_id
                    ).delete()
                    # Instances are a materialized view of the rows; leaving
                    # them behind renders a deleted event as a ghost chip.
                    s.query(EventInstanceRow).filter_by(
                        uid=uid, calendar_id=calendar_id
                    ).delete()
                    count += 1
                elif change.kind == "upsert" and change.event is not None:
                    local_event = dataclasses.replace(
                        change.event, calendar_id=calendar_id
                    )
                    # An override whose uid the backend could not resolve to the
                    # master's (Google omits iCalUID on cancelled instances).
                    # Recover it from the master's provider id, or the override
                    # lands under a uid with no master and produces no hole.
                    master_pid = getattr(change, "master_provider_id", None)
                    if master_pid and local_event.recurrence_id is not None:
                        master_row = (
                            s.query(EventRow)
                            .filter_by(
                                calendar_id=calendar_id,
                                provider_event_id=master_pid,
                                recurrence_id="",
                            )
                            .first()
                        )
                        if master_row is not None and master_row.uid != uid:
                            uid = master_row.uid
                            local_event = dataclasses.replace(local_event, uid=uid)
                    recurrence_id_str = (
                        local_event.recurrence_id.isoformat()
                        if local_event.recurrence_id
                        else ""
                    )
                    # Match the slot by instant, not by ISO spelling: the same
                    # occurrence is spelled differently by each provider and by
                    # the UI, and a string miss creates a duplicate row.
                    rkey = recurrence_key(
                        local_event.recurrence_id, all_day=local_event.all_day
                    )
                    row = _match_row(
                        s.query(EventRow).filter_by(uid=uid, calendar_id=calendar_id),
                        recurrence_id_str,
                        rkey,
                    )
                    if row is None and local_event.provider_event_id:
                        # Stale local-UUID uid from before mark_synced rewrote
                        # to canonical. Match on (calendar_id, provider_event_id)
                        # and adopt the canonical uid.
                        row = _match_row(
                            s.query(EventRow).filter_by(
                                calendar_id=calendar_id,
                                provider_event_id=local_event.provider_event_id,
                            ),
                            recurrence_id_str,
                            rkey,
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
                        # recurrence_id keeps the local spelling (it is part of
                        # the PK; the key is what identifies the slot), and
                        # local_exdates is locally owned — a server that knows
                        # nothing of our tombstones must not clear them.
                        _skip = {
                            "uid",
                            "calendar_id",
                            "recurrence_id",
                            "local_exdates",
                        }
                        for col_name in EventRow.__table__.columns.keys():  # noqa: SIM118
                            if col_name in _skip:
                                continue
                            # Keep local color when backend doesn't echo it back.
                            if col_name == "color" and updated.color is None:
                                continue
                            setattr(row, col_name, getattr(updated, col_name, None))
                        row.local_dirty = 0
                    # Either a master or one of its overrides can carry the
                    # evidence that a local tombstone is now redundant.
                    masters_touched.add((uid, calendar_id))
                    # Defer instance rebuilds to after this transaction commits so
                    # the write lock is held only for the fast EventRow upserts and
                    # cursor update, not the expensive iCal expansion.
                    masters_to_rebuild[(uid, calendar_id)] = local_event
                    count += 1

            # Reconcile local tombstones once every row in the batch is visible,
            # so a cancelled override arriving alongside its master counts.
            s.flush()
            for t_uid, t_cal in masters_touched:
                master_row = (
                    s.query(EventRow)
                    .filter_by(uid=t_uid, calendar_id=t_cal, recurrence_id="")
                    .first()
                )
                if master_row is None:
                    continue
                self._reconcile_local_exdates(s, master_row)
                if (t_uid, t_cal) in masters_to_rebuild:
                    masters_to_rebuild[(t_uid, t_cal)] = _row_to_event(master_row)
            if new_cursor_json:
                s.query(Calendar).filter(Calendar.id == calendar_id).update(
                    {"sync_cursor": new_cursor_json}
                )
        # EventRows are now committed. Rebuild event_instances for each master.
        # rebuild_batch_size=1 (default): one session per master so GUI writes
        # can interleave during incremental sync ticks.
        # rebuild_batch_size=0: one session for all masters — faster for bulk
        # imports where GUI interleaving is not needed (subscribe runs off-thread).
        masters = list(masters_to_rebuild.values())
        if rebuild_batch_size <= 0:
            with self._write_session() as s:
                for event in masters:
                    self._rebuild_instances_for(s, event)
        else:
            for i in range(0, len(masters), rebuild_batch_size):
                with self._write_session() as s:
                    for event in masters[i : i + rebuild_batch_size]:
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
            return q.order_by(Account.sort_order, Account.display_name).all()

    def set_account_orders(self, orders: list[tuple[str, int]]) -> None:
        from lilical.models.account import Account

        with self._write_session() as s:
            for acc_id, order in orders:
                acc = s.query(Account).filter(Account.id == acc_id).first()
                if acc is not None:
                    acc.sort_order = order

    def update_account(
        self,
        account_id: str,
        *,
        display_name: str | None = None,
        identity: str | None = None,
        server_url: str | None = None,
        enabled: bool | None = None,
        include_contacts: bool | None = None,
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
            if include_contacts is not None:
                acc.include_contacts = 1 if include_contacts else 0

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
                s.query(EventCompletionRow).filter(
                    EventCompletionRow.calendar_id.in_(cal_ids)
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

    def delete_calendar(self, calendar_id: str) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            s.query(EventInstanceRow).filter(
                EventInstanceRow.calendar_id == calendar_id
            ).delete(synchronize_session=False)
            s.query(EventCompletionRow).filter(
                EventCompletionRow.calendar_id == calendar_id
            ).delete(synchronize_session=False)
            s.query(EventRow).filter(EventRow.calendar_id == calendar_id).delete(
                synchronize_session=False
            )
            s.query(PendingOpRow).filter(
                PendingOpRow.calendar_id == calendar_id
            ).delete(synchronize_session=False)
            s.query(Calendar).filter(Calendar.id == calendar_id).delete(
                synchronize_session=False
            )

    def reset_sync_cursors(self, account_id: str) -> None:
        """Clear sync_cursor on all calendars so the next sync does a full resync."""
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            s.query(Calendar).filter(Calendar.account_id == account_id).update(
                {"sync_cursor": None}
            )

    def set_calendar_visibility(self, calendar_id: str, is_visible: bool) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            s.query(Calendar).filter(Calendar.id == calendar_id).update(
                {"is_visible": 1 if is_visible else 0}
            )
        self.cal_metadata_changed.emit(calendar_id)

    def set_calendar_inclusion(self, calendar_id: str, is_included: bool) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            s.query(Calendar).filter(Calendar.id == calendar_id).update(
                {"is_included": 1 if is_included else 0}
            )
        self.cal_metadata_changed.emit(calendar_id)

    def get_calendar(self, calendar_id: str):
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            return s.query(Calendar).filter(Calendar.id == calendar_id).first()

    def set_calendar_display_name(self, calendar_id: str, name: str) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            row = s.query(Calendar).filter(Calendar.id == calendar_id).first()
            if row is None:
                return
            row.display_name = name
        self.cal_metadata_changed.emit(calendar_id)

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
        # Piggy-back on events_changed so views that listen to it also re-render.
        self.events_changed.emit(calendar_id, set())
        self.cal_metadata_changed.emit(calendar_id)
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
        include_contacts: bool = False,
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
                    include_contacts=1 if include_contacts else 0,
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

    def list_calendars(self, account_id: str, included_only: bool = True) -> list[Any]:
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            q = s.query(Calendar).filter(Calendar.account_id == account_id)
            if included_only:
                q = q.filter(Calendar.is_included == 1)
            return q.order_by(Calendar.sort_order, Calendar.display_name).all()

    def set_calendar_orders(self, orders: list[tuple[str, int]]) -> None:
        from lilical.models.calendar import Calendar

        with self._write_session() as s:
            for cal_id, order in orders:
                cal = s.query(Calendar).filter(Calendar.id == cal_id).first()
                if cal is not None:
                    cal.sort_order = order

    def visible_calendar_ids(self) -> set[str]:
        """IDs of every visible calendar across every enabled account.

        Returned as a `set` (never None) so views can pass it directly as the
        `calendar_ids` filter to `list_instances`: a `set()` means "no visible
        calendars, render nothing" — explicitly distinct from `None` ("no
        filter, render everything").
        """
        from lilical.models.calendar import Calendar

        with Session(self._engine) as s:
            rows = (
                s.query(Calendar.id)
                .filter(Calendar.is_visible == 1, Calendar.is_included == 1)
                .all()
            )
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

            next_order = (
                s.query(func.max(Calendar.sort_order))
                .filter(Calendar.account_id == account_id)
                .scalar()
                or 0
            ) + 1

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
                            sort_order=next_order,
                        )
                    )
                    next_order += 1

    def create_subscription(
        self,
        *,
        canonical_source: str,
        display_name: str,
        color: str,
        events: list[Event],
        content_sha256: str,
        rebuild_batch_size: int = 1,
    ) -> str:
        """Create one Calendar row (read-only) under the singleton Subscriptions
        account, persisting *events* atomically. Returns the new calendar id.

        Auto-creates the Subscriptions account on first use.
        """
        import uuid

        from lilical.backends.subscription import (
            SUBSCRIPTION_ACCOUNT_ID,
            SUBSCRIPTION_ACCOUNT_NAME,
            SubscriptionCursor,
        )
        from lilical.models.account import Account

        calendar_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with self._write_session() as s:
            acc = s.query(Account).filter(Account.id == SUBSCRIPTION_ACCOUNT_ID).first()
            if acc is None:
                s.add(
                    Account(
                        id=SUBSCRIPTION_ACCOUNT_ID,
                        kind="subscription",
                        display_name=SUBSCRIPTION_ACCOUNT_NAME,
                        identity="",
                        server_url=None,
                        secret_ref="",
                        created_at=now,
                        enabled=1,
                        include_contacts=0,
                    )
                )
            s.add(
                Calendar(
                    id=calendar_id,
                    account_id=SUBSCRIPTION_ACCOUNT_ID,
                    provider_id=canonical_source,
                    display_name=display_name,
                    color=color,
                    is_primary=0,
                    is_visible=1,
                    is_included=1,
                    access_role="reader",
                )
            )

        # Persist the events using the same path the sync engine uses.
        # apply_remote_changes manages its own write session.
        from lilical.backends.base import EventChange

        changes = [EventChange(kind="upsert", event=e, uid=e.uid) for e in events]
        cursor = SubscriptionCursor(
            etag=None, last_modified=None, content_sha256=content_sha256
        )
        self.apply_remote_changes(
            calendar_id,
            changes,
            json.dumps(cursor.to_json()),
            rebuild_batch_size=rebuild_batch_size,
        )
        return calendar_id

    def delete_subscription(self, calendar_id: str) -> bool:
        """Delete a subscription. Returns True if the Subscriptions account was
        also removed (i.e., this was the last subscription)."""
        from lilical.backends.subscription import SUBSCRIPTION_ACCOUNT_ID
        from lilical.models.account import Account

        self.delete_calendar(calendar_id)
        with self._write_session() as s:
            remaining = (
                s.query(Calendar)
                .filter(Calendar.account_id == SUBSCRIPTION_ACCOUNT_ID)
                .count()
            )
            if remaining == 0:
                s.query(Account).filter(Account.id == SUBSCRIPTION_ACCOUNT_ID).delete(
                    synchronize_session=False
                )
                return True
        return False

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

        Post-cutoff EXDATEs and override rows are migrated onto the tail. Left
        on the truncated master they would be unreachable by its RRULE, and the
        expander appends overrides unconditionally — so previously-deleted
        occurrences came back and previously-edited ones rendered as ghosts
        detached from any series.

        Caveat: PATCHing a Graph seriesMaster's recurrence drops server-side
        exceptions on the truncated master. That is a provider limitation, not
        something this can repair locally.
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

            # Compute UNTIL = one second before the split point (inclusive
            # boundary). RFC 5545 requires UNTIL in UTC when DTSTART is tz-aware,
            # so convert before stamping 'Z' — split_at_dt may be in any zone.
            until_dt = (split_at_dt - timedelta(seconds=1)).astimezone(timezone.utc)
            until_str = until_dt.strftime("%Y%m%dT%H%M%SZ")

            # Update master RRULE: remove COUNT, set UNTIL
            rrule = master_event.rrule or ""
            rrule = _re.sub(r";?COUNT=\d+", "", rrule)
            rrule = _re.sub(r";?UNTIL=[^;]+", "", rrule)
            rrule = rrule.rstrip(";") + f";UNTIL={until_str}"

            master_row.rrule = rrule
            master_row.local_dirty = True

            # Partition exclusions at the cut. Anything at or after it belongs
            # to the tail; leaving it on the master silently un-deletes it.
            all_day = bool(master_row.all_day)
            cut_key = recurrence_key(split_at_dt, all_day=all_day)
            keep_exdates = tuple(
                d for d in master_event.exdates if _exdate_key(d, all_day) < cut_key
            )
            tail_exdates = tuple(
                d for d in master_event.exdates if _exdate_key(d, all_day) >= cut_key
            )
            master_row.exdates = _dt_tuple_to_json(
                _subtract_exdates(keep_exdates, master_event.local_exdates, all_day)
            )
            master_row.local_exdates = _dump_local_exdates(
                [
                    e
                    for e in _load_local_exdates(master_row.local_exdates)
                    if _entry_key(e, all_day) < cut_key
                ]
            )

            # Rebuild instances for the truncated master
            updated_master = _row_to_event(master_row)
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
                exdates=tail_exdates,
                local_exdates=(),
                local_dirty=True,
            )
            tail_row = _event_to_row(tail)
            tail_row.local_dirty = True
            s.add(tail_row)

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

            # Re-parent post-cutoff overrides onto the tail. Ops are drained
            # FIFO, so the create above lands (and mark_synced fills in the
            # tail's provider id) before these update_instance ops run.
            for ov_row in (
                s.query(EventRow)
                .filter(
                    EventRow.uid == uid,
                    EventRow.calendar_id == calendar_id,
                    EventRow.recurrence_id != "",
                    EventRow.recurrence_key >= cut_key,
                )
                .all()
            ):
                ov_row.uid = new_uid
                ov_row.provider_event_id = None
                ov_row.etag = None
                ov_row.local_dirty = True
                if account_id and not ov_row.deleted_locally:
                    s.add(
                        PendingOpRow(
                            account_id=account_id,
                            calendar_id=calendar_id,
                            uid=new_uid,
                            op="update_instance",
                            payload=_event_to_json(_row_to_event(ov_row)),
                            if_match=None,
                            created_at=_utc_now(),
                        )
                    )
            s.flush()
            self._rebuild_instances_for(s, tail)

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
            # RFC 5545 UNTIL must be UTC for tz-aware DTSTART; convert before 'Z'.
            cut = (until_dt - timedelta(seconds=1)).astimezone(timezone.utc)
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
