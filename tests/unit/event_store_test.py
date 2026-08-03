from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lilical.backends.base import EventChange
from lilical.models.account import Account
from lilical.models.calendar import Calendar
from lilical.models.db import Base
from lilical.models.event import Attendee, Event, EventRow
from lilical.models.pending_op import PendingOpRow
from lilical.models.setting import Setting
from lilical.storage.event_store import EventStore


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    _create_test_schema(engine)
    with Session(engine) as session, session.begin():
        session.add(
            Account(
                id="acc-1",
                kind="google",
                display_name="Work",
                identity="lili@example.com",
                secret_ref="google:acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="provider-cal-1",
                display_name="Work Calendar",
                color="#5e9fff",
                access_role="owner",
            )
        )
        session.add(Setting(key="schema_version", value="0001"))
    return engine


def _create_test_schema(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                display_name TEXT NOT NULL,
                identity TEXT NOT NULL,
                server_url TEXT,
                secret_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                include_contacts INTEGER DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE calendars (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                provider_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                color TEXT,
                is_primary INTEGER DEFAULT 0,
                is_visible INTEGER DEFAULT 1,
                is_included INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                is_favorite INTEGER DEFAULT 0,
                access_role TEXT,
                sync_cursor TEXT,
                last_synced_at TEXT,
                UNIQUE(account_id, provider_id)
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE events (
                uid TEXT NOT NULL,
                calendar_id TEXT NOT NULL REFERENCES calendars(id),
                recurrence_id TEXT NOT NULL DEFAULT '',
                recurrence_key INTEGER NOT NULL DEFAULT 0,
                provider_event_id TEXT,
                dtstart TEXT NOT NULL,
                dtend TEXT NOT NULL,
                tz TEXT NOT NULL,
                all_day INTEGER DEFAULT 0,
                summary TEXT DEFAULT '',
                description TEXT DEFAULT '',
                location TEXT DEFAULT '',
                url TEXT,
                rrule TEXT,
                exdates TEXT,
                local_exdates TEXT,
                rdates TEXT,
                attendees TEXT,
                organizer TEXT,
                categories TEXT,
                color TEXT,
                status TEXT DEFAULT 'CONFIRMED',
                self_response TEXT,
                transparency TEXT DEFAULT 'OPAQUE',
                valarms TEXT,
                etag TEXT,
                sequence INTEGER DEFAULT 0,
                last_modified TEXT,
                local_dirty INTEGER DEFAULT 0,
                deleted_locally INTEGER DEFAULT 0,
                conflict_state TEXT,
                local_modified_at TEXT,
                inserted_at TEXT,
                PRIMARY KEY(uid, calendar_id, recurrence_id),
                UNIQUE(calendar_id, provider_event_id, recurrence_id)
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE pending_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                calendar_id TEXT NOT NULL REFERENCES calendars(id),
                uid TEXT NOT NULL,
                op TEXT NOT NULL,
                payload TEXT NOT NULL,
                if_match TEXT,
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE event_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                calendar_id TEXT NOT NULL,
                dtstart_utc INTEGER NOT NULL,
                dtend_utc INTEGER NOT NULL,
                dtstart_local TEXT NOT NULL,
                dtend_local TEXT NOT NULL,
                all_day INTEGER DEFAULT 0,
                is_override INTEGER DEFAULT 0,
                recurrence_id TEXT NOT NULL DEFAULT '',
                recurrence_key INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE event_completions (
                calendar_id TEXT NOT NULL,
                uid TEXT NOT NULL,
                dtstart_utc INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY(calendar_id, uid, dtstart_utc)
            )
            """
        )


def test_model_metadata_creates_sqlite_schema() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)


def _event(**overrides: Any) -> Event:
    data: dict[str, Any] = {
        "uid": "event-1",
        "calendar_id": "cal-1",
        "provider_event_id": "provider-event-1",
        "dtstart": datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        "dtend": datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        "tz": "UTC",
        "summary": "Design review",
        "description": "Discuss the design",
        "location": "Room 3",
        "url": "https://meet.example/event-1",
        "rrule": "FREQ=WEEKLY;COUNT=2",
        "exdates": (datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),),
        "rdates": (datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),),
        "attendees": (Attendee(email="anna@example.com"),),
        "categories": ("work",),
        "color": "#ff0000",
        "valarms": ("TRIGGER:-PT10M",),
        "etag": '"etag-1"',
        "sequence": 3,
        "last_modified": datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return Event(**data)


def test_queue_create_persists_event_fields_and_pending_op(engine) -> None:
    store = EventStore(engine)
    event = _event()

    store.queue_create(event)

    with Session(engine) as session:
        row = session.query(EventRow).one()
        pending = session.query(PendingOpRow).one()

    assert row.dtstart == "2026-05-13T09:00:00+00:00"
    assert row.dtend == "2026-05-13T10:00:00+00:00"
    assert row.url == "https://meet.example/event-1"
    assert json.loads(row.exdates) == ["2026-05-20T09:00:00+00:00"]
    assert json.loads(row.attendees)[0]["email"] == "anna@example.com"
    assert row.local_dirty == 1
    assert pending.account_id == "acc-1"
    assert pending.calendar_id == "cal-1"
    assert pending.uid == "event-1"
    assert pending.op == "create"
    assert json.loads(pending.payload)["summary"] == "Design review"
    assert pending.if_match is None


def test_queue_update_adds_pending_op_with_previous_etag(engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            EventRow(
                uid="event-1",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="provider-event-1",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T10:00:00+00:00",
                tz="UTC",
                summary="Old title",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
    store = EventStore(engine)

    store.queue_update(_event(summary="New title"), prev_etag='"old-etag"')

    with Session(engine) as session:
        row = session.query(EventRow).one()
        pending = session.query(PendingOpRow).one()

    assert row.summary == "New title"
    assert row.local_dirty == 1
    assert pending.op == "update"
    assert pending.if_match == '"old-etag"'
    assert json.loads(pending.payload)["summary"] == "New title"


def test_get_event_round_trips_stored_fields(engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            EventRow(
                uid="event-1",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="provider-event-1",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T10:00:00+00:00",
                tz="UTC",
                all_day=0,
                summary="Design review",
                description="Discuss the design",
                location="Room 3",
                url="https://meet.example/event-1",
                rrule="FREQ=WEEKLY;COUNT=2",
                exdates=json.dumps(["2026-05-20T09:00:00+00:00"]),
                rdates=json.dumps(["2026-05-27T09:00:00+00:00"]),
                attendees=json.dumps(["anna@example.com"]),
                categories=json.dumps(["work"]),
                color="#ff0000",
                valarms=json.dumps(["TRIGGER:-PT10M"]),
                etag='"etag-1"',
                sequence=3,
                last_modified="2026-05-12T08:00:00+00:00",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    event = EventStore(engine).get_event("event-1", "cal-1")

    assert event is not None
    assert event.dtstart == datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    assert event.dtend == datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)
    assert event.url == "https://meet.example/event-1"
    assert event.rrule == "FREQ=WEEKLY;COUNT=2"
    assert event.exdates == (datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),)
    assert len(event.attendees) == 1 and event.attendees[0].email == "anna@example.com"
    assert event.categories == ("work",)
    assert event.valarms == ("TRIGGER:-PT10M",)
    assert event.last_modified == datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)


def test_create_account_creates_account_and_calendar(engine) -> None:
    """Bug 11: EventStore.create_account creates both Account and Calendar rows."""
    store = EventStore(engine)
    store.create_account(
        account_id="new-acc",
        kind="google",
        display_name="Test",
        identity="test@example.com",
        server_url=None,
        calendar_id="new-cal",
        calendar_display_name="Test Calendar",
    )

    with Session(engine) as s:
        accounts = s.query(Account).all()
        calendars = s.query(Calendar).all()
        cal = s.query(Calendar).filter(Calendar.id == "new-cal").first()

    assert len(accounts) == 2
    assert len(calendars) == 2
    new_acc = [a for a in accounts if a.id == "new-acc"][0]
    assert new_acc.kind == "google"
    assert new_acc.display_name == "Test"
    assert new_acc.identity == "test@example.com"
    assert new_acc.server_url is None
    assert new_acc.enabled == 1
    assert cal is not None
    assert cal.account_id == "new-acc"
    assert cal.display_name == "Test Calendar"
    assert cal.is_primary == 1
    assert cal.is_visible == 1


def test_update_account_modifies_named_fields_only(engine) -> None:
    store = EventStore(engine)

    store.update_account("acc-1", display_name="Renamed")

    acc = store.get_account("acc-1")
    assert acc.display_name == "Renamed"
    assert acc.identity == "lili@example.com"  # unchanged
    assert acc.server_url is None  # unchanged
    assert acc.enabled == 1  # unchanged

    store.update_account("acc-1", identity="new@example.com", server_url="https://x")
    acc = store.get_account("acc-1")
    assert acc.identity == "new@example.com"
    assert acc.server_url == "https://x"

    store.update_account("acc-1", enabled=False)
    acc = store.get_account("acc-1")
    assert acc.enabled == 0


def test_update_account_noop_for_missing_account(engine) -> None:
    store = EventStore(engine)
    store.update_account("does-not-exist", display_name="x")  # must not raise


def test_delete_account_cascades_through_calendars_events_and_pending_ops(
    engine,
) -> None:
    store = EventStore(engine)
    store.queue_create(_event())

    with Session(engine) as s:
        assert s.query(Account).count() == 1
        assert s.query(Calendar).count() == 1
        assert s.query(EventRow).count() == 1
        assert s.query(PendingOpRow).count() == 1

    store.delete_account("acc-1")

    with Session(engine) as s:
        assert s.query(Account).count() == 0
        assert s.query(Calendar).count() == 0
        assert s.query(EventRow).count() == 0
        assert s.query(PendingOpRow).count() == 0


def test_delete_account_only_removes_targeted_account(engine) -> None:
    store = EventStore(engine)
    store.create_account(
        account_id="acc-2",
        kind="caldav",
        display_name="Personal",
        identity="lili@personal",
        server_url=None,
        calendar_id="cal-2",
        calendar_display_name="Personal Calendar",
    )

    store.delete_account("acc-1")

    with Session(engine) as s:
        accounts = s.query(Account).all()
        calendars = s.query(Calendar).all()

    assert [a.id for a in accounts] == ["acc-2"]
    assert [c.id for c in calendars] == ["cal-2"]


def test_set_calendar_visibility_toggles_flag(engine) -> None:
    store = EventStore(engine)
    cals_before = store.list_calendars("acc-1", included_only=False)
    assert cals_before[0].is_visible == 1

    store.set_calendar_visibility("cal-1", False)
    cals = store.list_calendars("acc-1", included_only=False)
    assert cals[0].is_visible == 0

    store.set_calendar_visibility("cal-1", True)
    cals = store.list_calendars("acc-1", included_only=False)
    assert cals[0].is_visible == 1


def test_apply_remote_changes_uses_local_calendar_and_persists_cursor(engine) -> None:
    store = EventStore(engine)
    remote_event = _event(calendar_id="provider-cal-1")
    cursor_json = json.dumps({"type": "google", "sync_token": "next-token"})

    count = store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=remote_event, uid="event-1")],
        cursor_json,
    )

    with Session(engine) as session:
        row = session.query(EventRow).one()
        calendar = session.get(Calendar, "cal-1")

    assert count == 1
    assert row.calendar_id == "cal-1"
    assert calendar is not None
    assert calendar.sync_cursor == cursor_json


def _has_instance_at(engine, dt: datetime, uid: str = "event-1") -> bool:
    """True when a materialized occurrence exists at that instant."""
    from lilical.models.event import EventInstanceRow

    with Session(engine) as s:
        return (
            s.query(EventInstanceRow)
            .filter_by(uid=uid, dtstart_utc=int(dt.timestamp()))
            .count()
            > 0
        )


def test_apply_remote_changes_preserves_pending_delete_instance_exdate(engine) -> None:
    """A remote master upsert must not drop an EXDATE from a not-yet-pushed local
    delete_instance op (else the just-deleted occurrence resurrects)."""
    store = EventStore(engine)
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    master = _event(rrule="FREQ=WEEKLY;COUNT=4", exdates=(), rdates=())

    # Seed the master from the server (no EXDATEs yet).
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    # User deletes one occurrence locally → EXDATE on master + pending op.
    store.queue_delete_instance("event-1", "cal-1", rid)

    # A remote sync re-sends the master WITHOUT the EXDATE (e.g. Graph carries
    # none). The local deletion must survive.
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    kept = store.get_event("event-1", "cal-1").exdates
    assert any(abs((d - rid).total_seconds()) < 60 for d in kept)
    assert not _has_instance_at(engine, rid)


def test_delete_instance_survives_op_upload_and_remote_master_upsert(engine) -> None:
    """A local single-occurrence deletion must outlive its PendingOpRow.

    SyncEngine._tick pushes pending ops and deletes them in phase 1, then pulls
    incrementals in phase 2 of the SAME tick. Google and Graph masters carry no
    EXDATE for a cancelled occurrence, so if the local tombstone only lives as
    long as the pending op, that master upsert resurrects the occurrence the
    user just deleted. That is the reported bug: it disappears, then comes back.
    """
    store = EventStore(engine)
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    master = _event(rrule="FREQ=WEEKLY;COUNT=4", exdates=(), rdates=())

    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    store.queue_delete_instance("event-1", "cal-1", rid)
    assert not _has_instance_at(engine, rid)

    # The op uploads successfully and is reaped.
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).filter_by(op="delete_instance").delete()

    # The server echoes the master back unchanged — still no EXDATE, because
    # that is how Google and Graph represent a cancelled occurrence.
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )

    kept = store.get_event("event-1", "cal-1").exdates
    assert any(abs((d - rid).total_seconds()) < 60 for d in kept), (
        "local deletion was dropped once its pending op uploaded"
    )
    assert not _has_instance_at(engine, rid), "deleted occurrence resurrected"


def test_delete_instance_of_edited_occurrence_leaves_no_instance(engine) -> None:
    """Deleting an occurrence that was previously edited must remove it locally.

    queue_delete_instance rebuilds instances before tombstoning the override
    row, and the rebuild's override query filters deleted_locally == 0 — so the
    override is still visible to the rebuild and its instance row is re-emitted.
    """
    store = EventStore(engine)
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    master = _event(rrule="FREQ=WEEKLY;COUNT=4", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )

    # Edit that occurrence (moves it 30 minutes later), then delete it.
    edited = _event(
        rrule=None,
        exdates=(),
        rdates=(),
        dtstart=datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
    )
    store.queue_update_instance("event-1", "cal-1", rid, edited)
    assert _has_instance_at(engine, datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc))

    store.queue_delete_instance("event-1", "cal-1", rid)

    assert not _has_instance_at(engine, rid), "base occurrence still present"
    assert not _has_instance_at(
        engine, datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    ), "edited occurrence was re-emitted by the rebuild"


def test_split_series_moves_post_cutoff_exdates_to_tail(engine) -> None:
    """A deleted occurrence after the cut must not come back when the series splits.

    Exclusions left on the truncated master are unreachable by its shortened
    RRULE, so the occurrence silently reappears on the tail.
    """
    store = EventStore(engine)
    master = _event(rrule="FREQ=DAILY;COUNT=10", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    before_cut = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
    after_cut = datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)
    store.queue_delete_instance("event-1", "cal-1", before_cut)
    store.queue_delete_instance("event-1", "cal-1", after_cut)

    split_at = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    tail_uid = store.queue_split_series(
        "event-1",
        "cal-1",
        split_at,
        _event(uid="ignored", dtstart=split_at, rrule="FREQ=DAILY;COUNT=5"),
    )

    head = store.get_event("event-1", "cal-1")
    tail = store.get_event(tail_uid, "cal-1")
    assert any(abs((d - before_cut).total_seconds()) < 60 for d in head.exdates)
    assert not any(abs((d - after_cut).total_seconds()) < 60 for d in head.exdates)
    assert any(abs((d - after_cut).total_seconds()) < 60 for d in tail.exdates)
    assert not _has_instance_at(engine, after_cut, uid=tail_uid)


def test_split_series_reparents_post_cutoff_overrides(engine) -> None:
    """An edited occurrence after the cut must follow the tail, not orphan.

    The expander appends overrides unconditionally, so one left on the
    truncated master renders as a ghost detached from any series.
    """
    store = EventStore(engine)
    master = _event(rrule="FREQ=DAILY;COUNT=10", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    after_cut = datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)
    store.queue_update_instance(
        "event-1",
        "cal-1",
        after_cut,
        _event(rrule=None, exdates=(), rdates=(), summary="moved one"),
    )

    split_at = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    tail_uid = store.queue_split_series(
        "event-1",
        "cal-1",
        split_at,
        _event(uid="ignored", dtstart=split_at, rrule="FREQ=DAILY;COUNT=5"),
    )

    with Session(engine) as s:
        head_overrides = (
            s.query(EventRow)
            .filter(EventRow.uid == "event-1", EventRow.recurrence_id != "")
            .count()
        )
        tail_overrides = (
            s.query(EventRow)
            .filter(EventRow.uid == tail_uid, EventRow.recurrence_id != "")
            .all()
        )
    assert head_overrides == 0, "override orphaned on the truncated master"
    assert len(tail_overrides) == 1
    assert tail_overrides[0].summary == "moved one"


def test_truncate_series_classifies_overrides_by_instant(engine) -> None:
    """Overrides must be cut by instant, not by lexicographic ISO comparison.

    '2026-05-19T11:00:00+02:00' sorts after '2026-05-20T09:00:00+00:00' as a
    string while being the earlier instant, so a string compare kept the wrong
    ones.
    """
    store = EventStore(engine)
    master = _event(rrule="FREQ=DAILY;COUNT=10", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    # Same instants, spelled with a +02:00 offset as a provider might.
    before = datetime(2026, 5, 15, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    after = datetime(2026, 5, 20, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    for rid in (before, after):
        store.queue_update_instance(
            "event-1", "cal-1", rid, _event(rrule=None, exdates=(), rdates=())
        )

    store.queue_truncate_series(
        "event-1", "cal-1", datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    )

    with Session(engine) as s:
        remaining = [
            datetime.fromisoformat(r.recurrence_id)
            for r in s.query(EventRow)
            .filter(EventRow.uid == "event-1", EventRow.recurrence_id != "")
            .all()
        ]
    assert len(remaining) == 1, f"wrong overrides kept: {remaining}"
    assert remaining[0] == before


def test_remote_delete_removes_materialized_instances(engine) -> None:
    """A remote delete must clear event_instances too, or chips linger as ghosts."""
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    master = _event(rrule="FREQ=WEEKLY;COUNT=4", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    with Session(engine) as s:
        assert s.query(EventInstanceRow).filter_by(uid="event-1").count() > 0

    store.apply_remote_changes("cal-1", [EventChange(kind="delete", uid="event-1")], "")

    with Session(engine) as s:
        assert s.query(EventRow).filter_by(uid="event-1").count() == 0
        assert s.query(EventInstanceRow).filter_by(uid="event-1").count() == 0


def test_override_adopts_master_uid_from_master_provider_id(engine) -> None:
    """An override whose uid the backend couldn't resolve must find its master.

    Google omits iCalUID from cancelled-instance payloads, so the change
    arrives keyed on the instance id. Filed under that uid it has no master,
    _rebuild_instances_for bails out, and the deleted occurrence keeps showing.
    """
    store = EventStore(engine)
    master = _event(
        rrule="FREQ=WEEKLY;COUNT=4",
        exdates=(),
        rdates=(),
        provider_event_id="evt-rec",
    )
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)

    cancelled = _event(
        uid="evt-rec_20260520T090000Z",  # instance id, not the master's uid
        rrule=None,
        exdates=(),
        rdates=(),
        recurrence_id=rid,
        provider_event_id="evt-rec_20260520T090000Z",
        status="CANCELLED",
        dtstart=rid,
        dtend=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
    )
    store.apply_remote_changes(
        "cal-1",
        [
            EventChange(
                kind="upsert",
                event=cancelled,
                uid="evt-rec_20260520T090000Z",
                master_provider_id="evt-rec",
            )
        ],
        "",
    )

    with Session(engine) as s:
        overrides = (
            s.query(EventRow)
            .filter(EventRow.uid == "event-1", EventRow.recurrence_id != "")
            .all()
        )
    assert len(overrides) == 1, "override was not adopted under the master's uid"
    assert overrides[0].status == "CANCELLED"
    # And the cancelled override leaves a hole in the expansion.
    assert not _has_instance_at(engine, rid)


def _ui_recurrence_id(engine, uid: str, nth: int) -> datetime:
    """The recurrence_id the UI actually passes for the nth occurrence.

    Mirrors ui/views/week.py exactly: `fromisoformat(inst.dtstart_local)
    .astimezone()`. fromisoformat yields a fixed-offset tzinfo and astimezone()
    with no argument yields another one, so this is never a named zone — which
    is precisely what the ISO-string comparison sites never see in tests.
    """
    from lilical.models.event import EventInstanceRow

    with Session(engine) as s:
        rows = (
            s.query(EventInstanceRow)
            .filter_by(uid=uid)
            .order_by(EventInstanceRow.dtstart_utc)
            .all()
        )
        return datetime.fromisoformat(rows[nth].dtstart_local).astimezone()


@pytest.fixture
def system_tz(monkeypatch):
    """Run a test under a chosen system timezone."""
    import time as _time

    def _set(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        _time.tzset()
        monkeypatch.setattr("lilical.storage.event_store.local_iana_tz", lambda: name)

    yield _set
    _time.tzset()


@pytest.mark.parametrize(
    "sys_tz, event_tz",
    [
        ("America/New_York", "Europe/Paris"),
        ("Asia/Tokyo", "Europe/Paris"),
        ("Europe/Paris", "America/New_York"),
    ],
)
def test_delete_occurrence_end_to_end_non_utc(
    engine, system_tz, sys_tz, event_tz
) -> None:
    """Full UI-shaped path: a timed series in one zone, user in another.

    Every existing expander test uses UTC throughout, so the fixed-offset
    recurrence_id the UI really produces is never exercised.
    """
    system_tz(sys_tz)
    store = EventStore(engine)
    zone = ZoneInfo(event_tz)
    master = _event(
        rrule="FREQ=DAILY;COUNT=5",
        exdates=(),
        rdates=(),
        tz=event_tz,
        dtstart=datetime(2026, 5, 13, 11, 0, tzinfo=zone),
        dtend=datetime(2026, 5, 13, 12, 0, tzinfo=zone),
    )
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )

    rid = _ui_recurrence_id(engine, "event-1", 2)  # third occurrence
    store.queue_delete_instance("event-1", "cal-1", rid)

    assert not _has_instance_at(engine, rid)
    # And it must stay gone once the op uploads and the master echoes back.
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).filter_by(op="delete_instance").delete()
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    assert not _has_instance_at(engine, rid), "occurrence resurrected after sync"


def test_delete_occurrence_end_to_end_all_day(engine, system_tz) -> None:
    """Same, for an all-day series, from a non-UTC system zone."""
    system_tz("America/New_York")
    store = EventStore(engine)
    master = _event(
        rrule="FREQ=DAILY;COUNT=5",
        exdates=(),
        rdates=(),
        all_day=True,
        tz="UTC",
        dtstart=datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
    )
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )

    rid = _ui_recurrence_id(engine, "event-1", 2)
    store.queue_delete_instance("event-1", "cal-1", rid)

    assert not _has_instance_at(engine, rid)
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).filter_by(op="delete_instance").delete()
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )
    assert not _has_instance_at(engine, rid), "all-day occurrence resurrected"


def test_update_instance_matches_override_across_iso_spellings(engine) -> None:
    """recurrence_id identity must be by instant, not by ISO string.

    The UI always supplies a fixed-offset datetime (week.py does
    fromisoformat(...).astimezone(), which can never yield a named zone), while
    the provider stores its own spelling. Same instant, different string → an
    exact-string lookup misses and inserts a duplicate override row.
    """
    store = EventStore(engine)
    master = _event(rrule="FREQ=WEEKLY;COUNT=4", exdates=(), rdates=())
    store.apply_remote_changes(
        "cal-1", [EventChange(kind="upsert", event=master, uid="event-1")], ""
    )

    # Server-spelled override: 09:00+00:00.
    server_rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    store.queue_update_instance(
        "event-1",
        "cal-1",
        server_rid,
        _event(rrule=None, exdates=(), rdates=(), summary="first edit"),
    )

    # UI-spelled recurrence_id for the SAME instant: 11:00+02:00.
    ui_rid = datetime(2026, 5, 20, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    store.queue_update_instance(
        "event-1",
        "cal-1",
        ui_rid,
        _event(rrule=None, exdates=(), rdates=(), summary="second edit"),
    )

    with Session(engine) as s:
        overrides = (
            s.query(EventRow)
            .filter(EventRow.uid == "event-1", EventRow.recurrence_id != "")
            .all()
        )
    assert len(overrides) == 1, (
        f"duplicate override rows for one instant: "
        f"{[o.recurrence_id for o in overrides]}"
    )
    assert overrides[0].summary == "second edit"


# -- upsert_calendars (Bug: 400 from /me/calendars/default; placeholder cleanup) --


def test_upsert_calendars_inserts_new_calendar(engine) -> None:
    store = EventStore(engine)
    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "remote-cal-A", "display_name": "Holidays"}],
    )
    with Session(engine) as s:
        cals = s.query(Calendar).filter(Calendar.account_id == "acc-1").all()
    pids = {c.provider_id for c in cals}
    assert "remote-cal-A" in pids


def test_upsert_calendars_updates_display_name_when_changed(engine) -> None:
    store = EventStore(engine)
    # The fixture seeds a calendar with provider_id="provider-cal-1".
    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "provider-cal-1", "display_name": "Renamed"}],
    )
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "provider-cal-1").one()
    assert cal.display_name == "Renamed"


def test_upsert_calendars_removes_default_placeholder(engine) -> None:
    """Bug: Graph backend hit /me/calendars/default/calendarView/delta and 400'd
    because create_account inserts a stub row with provider_id='default'. After
    real calendars are discovered, the stub must be deleted."""
    store = EventStore(engine)
    with Session(engine) as s, s.begin():
        s.add(
            Calendar(
                id="placeholder",
                account_id="acc-1",
                provider_id="default",
                display_name="placeholder",
                color="#000",
                access_role="owner",
            )
        )

    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "AAMk-real-calendar-id", "display_name": "Real"}],
    )

    with Session(engine) as s:
        cals = s.query(Calendar).filter(Calendar.account_id == "acc-1").all()
    pids = {c.provider_id for c in cals}
    assert "default" not in pids
    assert "AAMk-real-calendar-id" in pids


def test_upsert_calendars_keeps_default_when_in_remote_list(engine) -> None:
    """Google uses provider_id='default'/'primary' as a real alias for the
    primary calendar — don't delete it in that case."""
    store = EventStore(engine)
    with Session(engine) as s, s.begin():
        # Clear the seeded row so we can put a 'default' one in.
        s.query(Calendar).delete()
        s.add(
            Calendar(
                id="placeholder",
                account_id="acc-1",
                provider_id="default",
                display_name="placeholder",
                color="#000",
                access_role="owner",
            )
        )

    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "default", "display_name": "Primary"}],
    )

    with Session(engine) as s:
        cals = s.query(Calendar).filter(Calendar.account_id == "acc-1").all()
    assert len(cals) == 1
    assert cals[0].provider_id == "default"
    assert cals[0].display_name == "Primary"


def test_upsert_calendars_skips_entries_with_empty_provider_id(engine) -> None:
    store = EventStore(engine)
    store.upsert_calendars(
        "acc-1",
        [
            {"provider_id": "", "display_name": "junk"},
            {"provider_id": None, "display_name": "more junk"},
            {"provider_id": "real", "display_name": "Real"},
        ],
    )
    with Session(engine) as s:
        cals = s.query(Calendar).filter(Calendar.account_id == "acc-1").all()
    pids = {c.provider_id for c in cals}
    assert pids >= {"real"}
    assert "" not in pids
    assert None not in pids


def test_upsert_calendars_preserves_visibility_on_existing_rows(engine) -> None:
    """Visibility toggles are user state; upsert must not stomp them when the
    server resends the same provider_id."""
    store = EventStore(engine)
    with Session(engine) as s, s.begin():
        cal = s.query(Calendar).filter(Calendar.provider_id == "provider-cal-1").one()
        cal.is_visible = 0

    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "provider-cal-1", "display_name": "Work Calendar"}],
    )

    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "provider-cal-1").one()
    assert cal.is_visible == 0


# -- calendar colour (server + palette fallback + user override) --------------


def test_upsert_calendars_stores_server_color_for_new_calendar(engine) -> None:
    store = EventStore(engine)
    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "remote-cal-A", "display_name": "Work", "color": "#228b22"}],
    )
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "remote-cal-A").one()
    assert cal.color == "#228b22"


def test_upsert_calendars_falls_back_to_palette_when_no_server_color(engine) -> None:
    """When the backend doesn't report a colour, we pick deterministically from
    the curated palette via sha1(provider_id) — same input always yields the
    same colour."""
    store = EventStore(engine)
    store.upsert_calendars(
        "acc-1",
        [{"provider_id": "no-color-cal", "display_name": "Anon"}],
    )
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "no-color-cal").one()
    assert cal.color in EventStore._FALLBACK_PALETTE
    # Determinism: re-deriving should give the same answer.
    assert cal.color == EventStore._fallback_color("no-color-cal")


def test_upsert_calendars_replaces_legacy_default_color_with_server_color(
    engine,
) -> None:
    """The legacy `#5e9fff` sentinel is treated as 'unset' — a server-provided
    colour replaces it on first sync."""
    store = EventStore(engine)
    # The fixture seeds a calendar with color="#5e9fff" (the legacy default).
    store.upsert_calendars(
        "acc-1",
        [
            {
                "provider_id": "provider-cal-1",
                "display_name": "Work",
                "color": "#216ffc",
            }
        ],
    )
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "provider-cal-1").one()
    assert cal.color == "#216ffc"


def test_upsert_calendars_preserves_user_overridden_color(engine) -> None:
    """If the user has set a non-default colour, a subsequent sync must not
    overwrite it with the server's value."""
    store = EventStore(engine)
    store.set_calendar_color("cal-1", "#ff00aa")
    store.upsert_calendars(
        "acc-1",
        [
            {
                "provider_id": "provider-cal-1",
                "display_name": "Work",
                "color": "#216ffc",  # server tries to change it; we must ignore.
            }
        ],
    )
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.provider_id == "provider-cal-1").one()
    assert cal.color == "#ff00aa"


def test_set_calendar_color_emits_events_changed(engine) -> None:
    store = EventStore(engine)
    captured: list[tuple[str, set]] = []
    store.events_changed.connect(lambda cid, uids: captured.append((cid, uids)))
    store.set_calendar_color("cal-1", "#abcdef")
    assert ("cal-1", set()) in captured
    with Session(engine) as s:
        cal = s.query(Calendar).filter(Calendar.id == "cal-1").one()
    assert cal.color == "#abcdef"


def test_get_calendar_returns_row(engine) -> None:
    store = EventStore(engine)
    cal = store.get_calendar("cal-1")
    assert cal is not None
    assert cal.provider_id == "provider-cal-1"
    assert store.get_calendar("nonexistent-id") is None


def test_fallback_palette_size_and_stability() -> None:
    assert len(EventStore._FALLBACK_PALETTE) == 12
    # sha1-based, so the answer is stable across processes (unlike hash()).
    a = EventStore._fallback_color("snail-id-1")
    b = EventStore._fallback_color("snail-id-1")
    assert a == b
    assert a in EventStore._FALLBACK_PALETTE


# ── queue_delete ─────────────────────────────────────────────────────────────


def test_queue_delete_soft_deletes_and_queues_pending_op(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event())

    store.queue_delete("event-1", "cal-1")

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="event-1").one()
        ops = s.query(PendingOpRow).all()

    assert row.deleted_locally == 1
    assert row.local_dirty == 1
    delete_ops = [op for op in ops if op.op == "delete"]
    assert len(delete_ops) == 1
    assert delete_ops[0].uid == "event-1"
    assert delete_ops[0].calendar_id == "cal-1"


def test_queue_delete_noop_for_missing_event(engine) -> None:
    store = EventStore(engine)
    # Should not raise even when the event doesn't exist.
    store.queue_delete("nonexistent", "cal-1")

    with Session(engine) as s:
        ops = s.query(PendingOpRow).all()
    assert ops == []


def test_queue_delete_removes_instances(engine) -> None:
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    with Session(engine) as s:
        before = s.query(EventInstanceRow).count()
    assert before == 1

    store.queue_delete("event-1", "cal-1")

    with Session(engine) as s:
        after = s.query(EventInstanceRow).count()
    assert after == 0


# ── apply_remote_changes signal emission ──────────────────────────────────────


def test_apply_remote_changes_emits_events_changed_signal(engine) -> None:
    store = EventStore(engine)
    captured: list[tuple[str, set]] = []
    store.events_changed.connect(lambda cid, uids: captured.append((cid, uids)))

    remote = _event(calendar_id="provider-cal-1")
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=remote, uid="event-1")],
        json.dumps({"type": "google", "sync_token": "t1"}),
    )

    assert len(captured) == 1
    cal_id, uids = captured[0]
    assert cal_id == "cal-1"
    assert "event-1" in uids


# ── list_instances range queries ──────────────────────────────────────────────


def test_list_instances_returns_events_in_range(engine) -> None:

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    start = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    results = store.list_instances(start, end)
    assert len(results) == 1
    assert results[0].uid == "event-1"


def test_list_instances_filters_by_calendar_ids(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    start = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)

    # Filtering with the right calendar_id returns it.
    results = store.list_instances(start, end, calendar_ids={"cal-1"})
    assert len(results) == 1

    # Filtering with a different calendar_id returns nothing.
    results = store.list_instances(start, end, calendar_ids={"cal-other"})
    assert len(results) == 0


def test_list_instances_excludes_events_outside_window(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    # Window entirely before the event
    before = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
    before_end = datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert store.list_instances(before, before_end) == []

    # Window entirely after the event
    after = datetime(2026, 5, 14, tzinfo=timezone.utc)
    after_end = datetime(2026, 5, 15, tzinfo=timezone.utc)
    assert store.list_instances(after, after_end) == []


# ── rebuild_all_instances ─────────────────────────────────────────────────────


def test_rebuild_all_instances_repopulates_instances(engine) -> None:
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    # Manually wipe instances to simulate corruption / migration.
    with Session(engine) as s, s.begin():
        s.query(EventInstanceRow).delete()

    with Session(engine) as s:
        assert s.query(EventInstanceRow).count() == 0

    store.rebuild_all_instances()

    with Session(engine) as s:
        assert s.query(EventInstanceRow).count() == 1


# ── recurring event store methods ─────────────────────────────────────────────


def test_queue_update_instance_upserts_override_row_and_enqueues_op(engine) -> None:
    from lilical.models.event import EventInstanceRow

    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-upd",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="AAMk-upd",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Standup",
                rrule="FREQ=WEEKLY;COUNT=4",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    recurrence_id_dt = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    edited = Event(
        uid="series-upd",
        calendar_id="cal-1",
        summary="Standup (rescheduled)",
        dtstart=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
        tz="UTC",
    )
    store.queue_update_instance("series-upd", "cal-1", recurrence_id_dt, edited)

    with Session(engine) as s:
        override_row = (
            s.query(EventRow)
            .filter_by(
                uid="series-upd",
                calendar_id="cal-1",
                recurrence_id=recurrence_id_dt.isoformat(),
            )
            .first()
        )
        assert override_row is not None
        assert override_row.summary == "Standup (rescheduled)"
        assert override_row.local_dirty == 1

        pending = s.query(PendingOpRow).one()
        assert pending.op == "update_instance"
        payload = json.loads(pending.payload)
        assert payload["recurrence_id"] == recurrence_id_dt.isoformat()

        override_inst = (
            s.query(EventInstanceRow)
            .filter_by(uid="series-upd", calendar_id="cal-1", is_override=1)
            .first()
        )
        assert override_inst is not None
        assert override_inst.recurrence_id == recurrence_id_dt.isoformat()


def test_queue_delete_instance_appends_exdate_and_enqueues_op(engine) -> None:
    from lilical.models.event import EventInstanceRow

    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-del",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="AAMk-del",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Weekly",
                rrule="FREQ=WEEKLY;COUNT=4",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    recurrence_id_dt = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    store.queue_delete_instance("series-del", "cal-1", recurrence_id_dt)

    with Session(engine) as s:
        master = (
            s.query(EventRow)
            .filter_by(uid="series-del", calendar_id="cal-1", recurrence_id="")
            .first()
        )
        assert master is not None
        # The deletion is a locally-owned tombstone, kept apart from the server's
        # EXDATE column so it can be reconciled away later. The master is not
        # marked dirty: no master update op is enqueued for it.
        assert master.local_dirty == 0
        assert master.exdates is None
        local = json.loads(master.local_exdates)
        assert [e["rid"] for e in local] == [recurrence_id_dt.isoformat()]
        # …and it reads back as an ordinary EXDATE for every consumer.
        assert recurrence_id_dt in store.get_event("series-del", "cal-1").exdates

        pending = s.query(PendingOpRow).one()
        assert pending.op == "delete_instance"
        payload = json.loads(pending.payload)
        assert payload["recurrence_id"] == recurrence_id_dt.isoformat()

        deleted_inst = (
            s.query(EventInstanceRow)
            .filter_by(uid="series-del", calendar_id="cal-1")
            .filter(EventInstanceRow.dtstart_utc == int(recurrence_id_dt.timestamp()))
            .first()
        )
        assert deleted_inst is None


def test_get_event_for_instance_returns_override_when_present(engine) -> None:
    from lilical.models.event import EventInstanceRow

    rid_iso = "2026-05-20T09:00:00+00:00"
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-gef",
                calendar_id="cal-1",
                recurrence_id="",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Master title",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-gef",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
                dtstart="2026-05-20T10:00:00+00:00",
                dtend="2026-05-20T10:30:00+00:00",
                tz="UTC",
                summary="Override title",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventInstanceRow(
                uid="series-gef",
                calendar_id="cal-1",
                dtstart_utc=int(
                    datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc).timestamp()
                ),
                dtend_utc=int(
                    datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc).timestamp()
                ),
                dtstart_local="2026-05-13T09:00:00+00:00",
                dtend_local="2026-05-13T09:30:00+00:00",
                recurrence_id="",
            )
        )
        s.add(
            EventInstanceRow(
                uid="series-gef",
                calendar_id="cal-1",
                dtstart_utc=int(
                    datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc).timestamp()
                ),
                dtend_utc=int(
                    datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc).timestamp()
                ),
                dtstart_local="2026-05-20T10:00:00+00:00",
                dtend_local="2026-05-20T10:30:00+00:00",
                is_override=1,
                recurrence_id=rid_iso,
            )
        )

    store = EventStore(engine)
    with Session(engine) as s:
        normal_inst = s.query(EventInstanceRow).filter_by(recurrence_id="").first()
        override_inst = (
            s.query(EventInstanceRow).filter_by(recurrence_id=rid_iso).first()
        )

    master_event = store.get_event_for_instance(normal_inst)
    assert master_event is not None
    assert master_event.summary == "Master title"

    override_event = store.get_event_for_instance(override_inst)
    assert override_event is not None
    assert override_event.summary == "Override title"
    assert override_event.recurrence_id is not None


def test_get_override_events_returns_non_deleted_overrides(engine) -> None:
    rid1_iso = "2026-05-20T09:00:00+00:00"
    rid2_iso = "2026-05-27T09:00:00+00:00"
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-ov",
                calendar_id="cal-1",
                recurrence_id="",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Master",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-ov",
                calendar_id="cal-1",
                recurrence_id=rid1_iso,
                dtstart="2026-05-20T10:00:00+00:00",
                dtend="2026-05-20T10:30:00+00:00",
                tz="UTC",
                summary="Override 1",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-ov",
                calendar_id="cal-1",
                recurrence_id=rid2_iso,
                dtstart="2026-05-27T10:00:00+00:00",
                dtend="2026-05-27T10:30:00+00:00",
                tz="UTC",
                summary="Override 2 (deleted)",
                deleted_locally=1,
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    overrides = store.get_override_events("series-ov", "cal-1")

    assert len(overrides) == 1
    assert overrides[0].summary == "Override 1"
    assert overrides[0].recurrence_id is not None


def test_rebuild_instances_for_override_delegates_to_master(engine) -> None:
    """apply_remote_changes on an override calls _rebuild_instances_for(override),
    which must delegate to the master and produce an is_override instance row."""
    from lilical.backends.base import EventChange
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    master_event = Event(
        uid="series-rb",
        calendar_id="cal-1",
        provider_event_id="AAMk-rb",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
        tz="UTC",
        summary="Master",
        rrule="FREQ=WEEKLY;COUNT=3",
    )
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=master_event, uid="series-rb")],
        "{}",
    )

    # Now apply an override — _rebuild_instances_for(override) should delegate
    # to the master and regenerate all instances, including an override row.
    override_event = Event(
        uid="series-rb",
        calendar_id="cal-1",
        recurrence_id=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        dtstart=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc),
        tz="UTC",
        summary="Master (rescheduled)",
    )
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=override_event, uid="series-rb")],
        "{}",
    )

    with Session(engine) as s:
        instances = (
            s.query(EventInstanceRow)
            .filter_by(uid="series-rb", calendar_id="cal-1")
            .all()
        )

    assert len(instances) >= 1
    override_insts = [i for i in instances if i.is_override == 1]
    assert len(override_insts) == 1
    modified_ts = int(datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc).timestamp())
    assert override_insts[0].dtstart_utc == modified_ts


# ── mark_synced ───────────────────────────────────────────────────────────────


def test_mark_synced_stores_provider_id_and_clears_dirty(engine) -> None:
    """After mark_synced, provider_event_id and etag are updated; local_dirty=0."""
    store = EventStore(engine)
    store.queue_create(_event(uid="ev-ms", provider_event_id=None, etag=None))

    store.mark_synced(
        "ev-ms",
        "cal-1",
        canonical_uid=None,
        provider_event_id="server-pid",
        etag='"new-etag"',
        sequence=1,
    )

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="ev-ms").one()

    assert row.provider_event_id == "server-pid"
    assert row.etag == '"new-etag"'
    assert row.sequence == 1
    assert row.local_dirty == 0


def test_mark_synced_does_not_clobber_dtstart(engine) -> None:
    """mark_synced must not overwrite dtstart/dtend with nulls."""
    store = EventStore(engine)
    store.queue_create(
        _event(
            uid="ev-ms2",
            provider_event_id=None,
            dtstart=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            dtend=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        )
    )

    store.mark_synced(
        "ev-ms2",
        "cal-1",
        canonical_uid=None,
        provider_event_id="pid",
        etag='"e"',
        sequence=0,
    )

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="ev-ms2").one()

    assert "2026-06-01" in row.dtstart
    assert "2026-06-01" in row.dtend


def test_mark_synced_no_op_for_missing_event(engine) -> None:
    """mark_synced on a nonexistent uid must not raise."""
    EventStore(engine).mark_synced(
        "no-such-uid",
        "cal-1",
        canonical_uid=None,
        provider_event_id="pid",
        etag=None,
        sequence=0,
    )


def test_mark_synced_rewrites_uid_with_cascade(engine) -> None:
    """When canonical_uid differs from local_uid, mark_synced cascades to all rows."""
    store = EventStore(engine)
    store.queue_create(_event(uid="local-uuid", provider_event_id=None))
    # Create an override row (recurrence_id set)

    from lilical.models.event import Event as _Event

    override = _Event(
        uid="local-uuid",
        calendar_id="cal-1",
        recurrence_id=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        summary="Override",
        dtstart=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        tz="UTC",
    )
    with Session(engine) as s, s.begin():
        from lilical.storage.event_store import _event_to_row

        s.add(_event_to_row(override))
        s.add(
            PendingOpRow(
                account_id="acc-1",
                calendar_id="cal-1",
                uid="local-uuid",
                op="update",
                payload="{}",
                created_at="2026-06-01T00:00:00",
            )
        )

    store.mark_synced(
        "local-uuid",
        "cal-1",
        canonical_uid="canonical-uuid",
        provider_event_id="AAMk-123",
        etag='"e1"',
        sequence=1,
    )

    with Session(engine) as s:
        rows = s.query(EventRow).filter_by(calendar_id="cal-1").all()
        ops = s.query(PendingOpRow).filter_by(calendar_id="cal-1").all()

    assert all(r.uid == "canonical-uuid" for r in rows), (
        f"Expected uid=canonical-uuid, got {[(r.uid, r.recurrence_id) for r in rows]}"
    )
    assert all(o.uid == "canonical-uuid" for o in ops)


def test_mark_synced_no_uid_change_when_canonical_matches(engine) -> None:
    """When canonical_uid == local_uid, no cascade runs."""
    store = EventStore(engine)
    store.queue_create(_event(uid="same-uuid", provider_event_id="pid"))

    store.mark_synced(
        "same-uuid",
        "cal-1",
        canonical_uid="same-uuid",
        provider_event_id="pid",
        etag='"e2"',
        sequence=2,
    )

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="same-uuid").one()
    assert row.uid == "same-uuid"
    assert row.local_dirty == 0


def test_apply_remote_changes_falls_back_to_provider_event_id(engine) -> None:
    """apply_remote_changes matches by provider_event_id when uid differs."""
    store = EventStore(engine)
    # Insert a row with a local-UUID uid but with provider_event_id set
    store.queue_create(
        _event(
            uid="local-uuid",
            provider_event_id="AAMk-graph-id",
        )
    )
    # Clear the pending op so it doesn't interfere
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    # Apply a remote upsert with the canonical uid (Graph id)
    remote_event = Event(
        uid="AAMk-graph-id",
        calendar_id="cal-1",
        provider_event_id="AAMk-graph-id",
        summary="Synced summary",
        dtstart=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        tz="UTC",
    )
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=remote_event, uid="AAMk-graph-id")],
        "{}",
    )

    with Session(engine) as s:
        rows = s.query(EventRow).filter_by(calendar_id="cal-1").all()

    assert len(rows) == 1
    assert rows[0].uid == "AAMk-graph-id", (
        f"Expected uid rewrite to AAMk-graph-id, got {rows[0].uid!r}"
    )
    assert rows[0].summary == "Synced summary"


# ── queue_split_series ────────────────────────────────────────────────────────


def _recurring_event(**overrides: Any) -> Event:
    base: dict[str, Any] = {
        "uid": "series-uid",
        "calendar_id": "cal-1",
        "provider_event_id": "pid-master",
        "etag": '"master-etag"',
        "sequence": 2,
        "dtstart": datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc),
        "dtend": datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
        "tz": "UTC",
        "summary": "Original title",
        "rrule": "FREQ=WEEKLY;COUNT=8",
    }
    base.update(overrides)
    return Event(**base)


def test_queue_split_series_truncates_master_rrule(engine) -> None:
    """Master RRULE gets UNTIL set; COUNT is stripped."""
    store = EventStore(engine)
    store.queue_create(_recurring_event())
    # Clear create op so we can count cleanly
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    split_at = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
    edited = _recurring_event(summary="New from here", rrule="FREQ=WEEKLY")
    store.queue_split_series("series-uid", "cal-1", split_at, edited)

    with Session(engine) as s:
        master_row = (
            s.query(EventRow).filter_by(uid="series-uid", recurrence_id="").one()
        )
        ops = s.query(PendingOpRow).order_by(PendingOpRow.created_at).all()

    assert "UNTIL=" in master_row.rrule
    assert "COUNT=" not in master_row.rrule
    # Two pending ops: update for master, create for tail
    op_types = {op.op for op in ops}
    assert "update" in op_types
    assert "create" in op_types


def test_queue_split_series_creates_tail_event(engine) -> None:
    """The new tail series has a fresh uid and starts at split_at."""
    store = EventStore(engine)
    store.queue_create(_recurring_event())
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    split_at = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
    edited = _recurring_event(summary="Tail title", rrule="FREQ=WEEKLY")
    new_uid = store.queue_split_series("series-uid", "cal-1", split_at, edited)

    with Session(engine) as s:
        tail_row = s.query(EventRow).filter_by(uid=new_uid, recurrence_id="").first()

    assert tail_row is not None
    assert tail_row.uid != "series-uid"
    assert "2026-06-16" in tail_row.dtstart


def test_queue_split_series_converts_count_to_until(engine) -> None:
    """COUNT-based RRULE is converted to UNTIL on split."""
    store = EventStore(engine)
    store.queue_create(_recurring_event(rrule="FREQ=DAILY;COUNT=20"))
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    split_at = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    store.queue_split_series("series-uid", "cal-1", split_at, _recurring_event())

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="series-uid", recurrence_id="").one()

    assert "COUNT=" not in row.rrule
    assert "UNTIL=" in row.rrule


# ── queue_truncate_series ─────────────────────────────────────────────────────


def test_queue_truncate_series_sets_until(engine) -> None:
    """Truncation appends UNTIL; COUNT is stripped; no tail series is created."""
    store = EventStore(engine)
    store.queue_create(_recurring_event(rrule="FREQ=WEEKLY;COUNT=10"))
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    until_dt = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    store.queue_truncate_series("series-uid", "cal-1", until_dt)

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="series-uid", recurrence_id="").one()
        ops = s.query(PendingOpRow).all()

    assert "UNTIL=" in row.rrule
    assert "COUNT=" not in row.rrule
    # Only one op (update master), no create for tail
    assert len(ops) == 1
    assert ops[0].op == "update"


def test_queue_split_series_until_is_utc_for_non_utc_split(engine) -> None:
    """RFC 5545 requires UNTIL in UTC for a tz-aware DTSTART. Splitting at
    21:00 NZST (UTC+12) must write UNTIL as the UTC instant one second earlier
    (08:59:59Z), not the local wall-clock mislabeled as Z."""
    nz = ZoneInfo("Pacific/Auckland")
    store = EventStore(engine)
    store.queue_create(
        _recurring_event(
            dtstart=datetime(2026, 6, 2, 21, 0, tzinfo=nz),
            dtend=datetime(2026, 6, 2, 22, 0, tzinfo=nz),
            tz="Pacific/Auckland",
            rrule="FREQ=WEEKLY;COUNT=8",
        )
    )
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    split_at = datetime(2026, 6, 16, 21, 0, tzinfo=nz)
    store.queue_split_series(
        "series-uid", "cal-1", split_at, _recurring_event(rrule="FREQ=WEEKLY")
    )

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="series-uid", recurrence_id="").one()

    # 2026-06-16 21:00 NZST == 2026-06-16 09:00 UTC; minus one second.
    assert "UNTIL=20260616T085959Z" in row.rrule, row.rrule


def test_queue_truncate_series_until_is_utc_for_non_utc(engine) -> None:
    """Truncate twin of the non-UTC UNTIL test."""
    nz = ZoneInfo("Pacific/Auckland")
    store = EventStore(engine)
    store.queue_create(
        _recurring_event(
            dtstart=datetime(2026, 6, 2, 21, 0, tzinfo=nz),
            dtend=datetime(2026, 6, 2, 22, 0, tzinfo=nz),
            tz="Pacific/Auckland",
            rrule="FREQ=WEEKLY;COUNT=10",
        )
    )
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    until_dt = datetime(2026, 6, 9, 21, 0, tzinfo=nz)
    store.queue_truncate_series("series-uid", "cal-1", until_dt)

    with Session(engine) as s:
        row = s.query(EventRow).filter_by(uid="series-uid", recurrence_id="").one()

    assert "UNTIL=20260609T085959Z" in row.rrule, row.rrule


def test_queue_split_series_enqueues_update_master_and_create_tail(engine) -> None:
    """BUG 5 coverage: split must enqueue an update op for the truncated master
    (UNTIL present, COUNT gone) and a create op for the tail (fresh uid, dtstart
    at the split point, no provider id / recurrence id)."""
    store = EventStore(engine)
    store.queue_create(_recurring_event(rrule="FREQ=WEEKLY;COUNT=8"))
    with Session(engine) as s, s.begin():
        s.query(PendingOpRow).delete()

    split_at = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
    new_uid = store.queue_split_series(
        "series-uid", "cal-1", split_at, _recurring_event(rrule="FREQ=WEEKLY")
    )

    with Session(engine) as s:
        ops = {op.op: op for op in s.query(PendingOpRow).all()}
        tail_row = s.query(EventRow).filter_by(uid=new_uid, recurrence_id="").one()

    assert "update" in ops and "create" in ops
    update_payload = json.loads(ops["update"].payload)
    assert "UNTIL=" in update_payload["rrule"]
    assert "COUNT=" not in update_payload["rrule"]

    create_payload = json.loads(ops["create"].payload)
    assert create_payload["uid"] == new_uid != "series-uid"
    assert create_payload["provider_event_id"] is None
    assert create_payload.get("recurrence_id") in (None, "")
    assert "2026-06-16" in create_payload["dtstart"]
    assert "2026-06-16" in tail_row.dtstart


def test_all_day_event_instance_stores_correct_local_date(engine) -> None:
    """End-to-end: an all-day event whose dtstart is a local-zone midnight must
    produce an instance whose dtstart_local carries the intended calendar date —
    regardless of how it arrived (UTC midnight, VALUE=DATE naive midnight, etc.)
    or of the timezone the test happens to run in."""
    from lilical.models.event import EventInstanceRow

    ny_zone = ZoneInfo("America/New_York")
    # Simulate what _vevent_to_event returns after the CalDAV fix for a
    # server that emits DTSTART:20260704T000000Z — local midnight in NY.
    all_day_event = Event(
        uid="allday-roundtrip",
        calendar_id="cal-1",
        provider_event_id="allday-rt",
        dtstart=datetime(2026, 7, 4, 0, 0, tzinfo=ny_zone),
        dtend=datetime(2026, 7, 5, 0, 0, tzinfo=ny_zone),
        tz="America/New_York",
        all_day=True,
        summary="Independence Day",
    )

    store = EventStore(engine)
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=all_day_event, uid="allday-roundtrip")],
        "{}",
    )

    from sqlalchemy.orm import Session

    with Session(engine) as s:
        inst = s.query(EventInstanceRow).filter_by(uid="allday-roundtrip").one()
        assert inst.all_day == 1
        # dtstart_local already encodes the event's own local wall-clock date;
        # read it directly rather than reinterpreting it in the machine's
        # timezone (astimezone()), which would roll the date back a day when the
        # test runs west of the event's zone.
        parsed = datetime.fromisoformat(inst.dtstart_local)
        assert parsed.date() == date(2026, 7, 4), (
            f"Expected July 4 but got {parsed.date()} "
            f"(dtstart_local={inst.dtstart_local!r})"
        )


# ── _ensure_aware_dt edge cases ───────────────────────────────────────────────


def test_ensure_aware_dt_with_date(engine) -> None:
    """_ensure_aware_dt wraps a date into a UTC-midnight datetime."""
    from datetime import date as _date_cls

    d = _date_cls(2026, 7, 4)
    result = EventStore._ensure_aware_dt(d)
    assert isinstance(result, datetime)
    assert result.tzinfo == timezone.utc
    assert result.date() == d


def test_ensure_aware_dt_with_naive_datetime(engine) -> None:
    """_ensure_aware_dt treats naive datetimes as UTC."""
    naive = datetime(2026, 7, 4, 9, 0)
    result = EventStore._ensure_aware_dt(naive)
    assert result.tzinfo == timezone.utc
    assert result.hour == 9


def test_ensure_aware_dt_aware_passthrough(engine) -> None:
    """_ensure_aware_dt passes through aware datetimes unchanged."""
    aware = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)
    result = EventStore._ensure_aware_dt(aware)
    assert result is aware


def test_ensure_aware_dt_non_datetime_passthrough(engine) -> None:
    """_ensure_aware_dt returns non-datetime values as-is."""
    result = EventStore._ensure_aware_dt(None)
    assert result is None


# ── _json_dumps None branch ────────────────────────────────────────────────────


def test_json_dumps_none(engine) -> None:
    """_json_dumps(None) returns None."""
    from lilical.storage.event_store import _json_dumps

    assert _json_dumps(None) is None
    assert _json_dumps(["a"]) == '["a"]'


# ── queue_move ─────────────────────────────────────────────────────────────────


def test_queue_move_between_calendars(engine) -> None:
    """Move an event from cal-1 to a new cal-2."""
    from sqlalchemy.orm import Session

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    # Create target calendar
    with Session(engine) as s, s.begin():
        s.add(
            Calendar(
                id="cal-2",
                account_id="acc-1",
                provider_id="provider-cal-2",
                display_name="Target Calendar",
                color="#ff0000",
                access_role="owner",
            )
        )

    new_uid = store.queue_move(
        "event-1",
        "cal-1",
        "cal-2",
        _event(summary="Moved to cal-2", rrule=None),
    )

    with Session(engine) as s:
        old_rows = s.query(EventRow).filter_by(uid="event-1", calendar_id="cal-1").all()
        new_rows = s.query(EventRow).filter_by(uid=new_uid, calendar_id="cal-2").all()
        ops = s.query(PendingOpRow).all()

    assert len(old_rows) == 1
    assert old_rows[0].deleted_locally == 1
    assert old_rows[0].local_dirty == 1

    assert len(new_rows) == 1
    assert new_rows[0].summary == "Moved to cal-2"
    assert new_rows[0].local_dirty == 1

    op_types = {(op.op, op.calendar_id) for op in ops}
    assert ("delete", "cal-1") in op_types
    assert ("create", "cal-2") in op_types


def test_queue_move_nonexistent_old_event(engine) -> None:
    """Move of a nonexistent event doesn't crash."""
    from sqlalchemy.orm import Session

    store = EventStore(engine)
    with Session(engine) as s, s.begin():
        s.add(
            Calendar(
                id="cal-move2",
                account_id="acc-1",
                provider_id="cal-move2",
                display_name="Target",
                color="#000",
                access_role="owner",
            )
        )

    ev = _event(uid="ghost")
    new_uid = store.queue_move("ghost", "cal-1", "cal-move2", ev)
    with Session(engine) as s:
        new_rows = (
            s.query(EventRow).filter_by(uid=new_uid, calendar_id="cal-move2").all()
        )
    assert len(new_rows) == 1


# ── events_for_instances ──────────────────────────────────────────────────────


def test_events_for_instances_returns_mapping(engine) -> None:
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    with Session(engine) as s:
        instance = s.query(EventInstanceRow).one()

    mapping = store.events_for_instances([instance])
    assert id(instance) in mapping
    assert mapping[id(instance)].uid == "event-1"


def test_events_for_instances_empty(engine) -> None:
    store = EventStore(engine)
    assert store.events_for_instances([]) == {}


def test_events_for_instances_prefers_override(engine) -> None:
    from lilical.models.event import EventInstanceRow

    rid_iso = "2026-05-20T09:00:00+00:00"
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-evi",
                calendar_id="cal-1",
                recurrence_id="",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Master",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-evi",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
                dtstart="2026-05-20T10:00:00+00:00",
                dtend="2026-05-20T10:30:00+00:00",
                tz="UTC",
                summary="Override",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventInstanceRow(
                uid="series-evi",
                calendar_id="cal-1",
                dtstart_utc=1000000,
                dtend_utc=1000060,
                dtstart_local="2026-05-20T10:00:00+00:00",
                dtend_local="2026-05-20T10:30:00+00:00",
                is_override=1,
                recurrence_id=rid_iso,
            )
        )
        s.add(
            EventInstanceRow(
                uid="series-evi",
                calendar_id="cal-1",
                dtstart_utc=900000,
                dtend_utc=900060,
                dtstart_local="2026-05-13T09:00:00+00:00",
                dtend_local="2026-05-13T09:30:00+00:00",
                recurrence_id="",
            )
        )

    store = EventStore(engine)
    with Session(engine) as s:
        instances = s.query(EventInstanceRow).all()

    mapping = store.events_for_instances(instances)
    assert len(mapping) == 2

    for inst in instances:
        ev = mapping[id(inst)]
        if inst.recurrence_id:
            assert ev.summary == "Override"
        else:
            assert ev.summary == "Master"


# ── list_events_in_range ──────────────────────────────────────────────────────


def test_list_events_in_range_returns_matching(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    results = store.list_events_in_range(start, end)
    assert len(results) == 1
    assert results[0].uid == "event-1"


def test_list_events_in_range_excludes_outside(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    results = store.list_events_in_range(start, end)
    assert len(results) == 0


def test_list_events_in_range_filters_by_calendar_ids(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    results = store.list_events_in_range(start, end, calendar_ids={"cal-1"})
    assert len(results) == 1
    results = store.list_events_in_range(start, end, calendar_ids={"other"})
    assert len(results) == 0


# ── apply_remote_changes delete ────────────────────────────────────────────────


def test_apply_remote_changes_delete_removes_row(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    count = store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="delete", uid="event-1")],
        "{}",
    )

    with Session(engine) as s:
        rows = s.query(EventRow).all()
    assert count == 1
    assert len(rows) == 0


def test_apply_remote_changes_delete_missing_is_safe(engine) -> None:
    store = EventStore(engine)
    count = store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="delete", uid="nonexistent")],
        "{}",
    )
    assert count == 1  # still counted as processed


# ── queue_update_instance update existing override ────────────────────────────


def test_queue_update_instance_updates_existing_override(engine) -> None:
    """queue_update_instance on an existing override row updates in-place."""
    rid_dt = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    rid_iso = rid_dt.isoformat()
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-upd-existing",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="AAMk-upd-e",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Master",
                rrule="FREQ=WEEKLY;COUNT=4",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-upd-existing",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
                dtstart="2026-05-20T09:00:00+00:00",
                dtend="2026-05-20T09:30:00+00:00",
                tz="UTC",
                summary="Old override",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    edited = Event(
        uid="series-upd-existing",
        calendar_id="cal-1",
        summary="Updated override",
        dtstart=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
        tz="UTC",
    )
    store.queue_update_instance("series-upd-existing", "cal-1", rid_dt, edited)

    with Session(engine) as s:
        override_row = (
            s.query(EventRow)
            .filter_by(
                uid="series-upd-existing",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
            )
            .first()
        )
    assert override_row is not None
    assert override_row.summary == "Updated override"
    assert override_row.local_dirty == 1
    assert override_row.dtstart == "2026-05-20T10:00:00+00:00"


# ── queue_delete_instance with existing override row ──────────────────────────


def test_queue_delete_instance_marks_existing_override(engine) -> None:
    """queue_delete_instance also marks an existing override row as deleted."""
    rid_dt = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    rid_iso = rid_dt.isoformat()
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="series-del-ov",
                calendar_id="cal-1",
                recurrence_id="",
                provider_event_id="AAMk-del-ov",
                dtstart="2026-05-13T09:00:00+00:00",
                dtend="2026-05-13T09:30:00+00:00",
                tz="UTC",
                summary="Master",
                rrule="FREQ=WEEKLY;COUNT=4",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )
        s.add(
            EventRow(
                uid="series-del-ov",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
                dtstart="2026-05-20T10:00:00+00:00",
                dtend="2026-05-20T10:30:00+00:00",
                tz="UTC",
                summary="Override to delete",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    store.queue_delete_instance("series-del-ov", "cal-1", rid_dt)

    with Session(engine) as s:
        override_row = (
            s.query(EventRow)
            .filter_by(
                uid="series-del-ov",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
            )
            .first()
        )
    assert override_row is not None
    assert override_row.deleted_locally == 1
    assert override_row.local_dirty == 1
    # The exclusion lives in the local tombstone list, and surfaces through the
    # effective exdates every consumer reads.
    master_local = json.loads(
        s.query(EventRow)
        .filter_by(uid="series-del-ov", calendar_id="cal-1", recurrence_id="")
        .first()
        .local_exdates
        or "[]"
    )
    assert [e["rid"] for e in master_local] == [rid_iso]
    assert (
        datetime.fromisoformat(rid_iso)
        in store.get_event("series-del-ov", "cal-1").exdates
    )


# ── get_event edge cases ──────────────────────────────────────────────────────


def test_get_event_nonexistent_uid_returns_none(engine) -> None:
    store = EventStore(engine)
    assert store.get_event("nonexistent", "cal-1") is None


def test_get_event_falls_back_to_override_row(engine) -> None:
    """When only an override row exists (no master), get_event finds it."""
    rid_iso = "2026-05-20T09:00:00+00:00"
    with Session(engine) as s, s.begin():
        s.add(
            EventRow(
                uid="override-only",
                calendar_id="cal-1",
                recurrence_id=rid_iso,
                dtstart="2026-05-20T10:00:00+00:00",
                dtend="2026-05-20T10:30:00+00:00",
                tz="UTC",
                summary="Only override",
                inserted_at="2026-05-13T00:00:00+00:00",
            )
        )

    store = EventStore(engine)
    event = store.get_event("override-only", "cal-1")
    assert event is not None
    assert event.summary == "Only override"
    assert event.recurrence_id is not None


# ── _rebuild_instances_for early return ────────────────────────────────────────


def test_rebuild_instances_for_no_dtstart(engine) -> None:
    """_rebuild_instances_for returns early when dtstart is None."""
    from lilical.models.event import Event as _Event

    store = EventStore(engine)
    event = _Event(uid="no-dt", calendar_id="cal-1")
    # This must not raise despite dtstart being None.
    with Session(engine) as s, s.begin():
        store._rebuild_instances_for(s, event)


# ── recurring all-day _anchor_all_day ──────────────────────────────────────────


def test_recurring_all_day_event_calls_anchor(engine) -> None:
    """A recurring all-day event triggers _anchor_all_day in instance building."""
    from lilical.models.event import EventInstanceRow

    ny_zone = ZoneInfo("America/New_York")
    event = Event(
        uid="rec-allday",
        calendar_id="cal-1",
        provider_event_id="rec-allday-pid",
        dtstart=datetime(2026, 7, 4, 0, 0, tzinfo=ny_zone),
        dtend=datetime(2026, 7, 5, 0, 0, tzinfo=ny_zone),
        tz="America/New_York",
        all_day=True,
        summary="All-day recurring",
        rrule="FREQ=DAILY;COUNT=3",
    )
    store = EventStore(engine)
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=event, uid="rec-allday")],
        "{}",
    )

    with Session(engine) as s:
        instances = s.query(EventInstanceRow).filter_by(uid="rec-allday").all()
    assert len(instances) == 3
    for inst in instances:
        assert inst.all_day == 1


# ── list_pending_ops / delete_pending_op / get_pending_op ──────────────────────


def test_list_pending_ops_returns_ops_for_account(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    ops = store.list_pending_ops("acc-1")
    assert len(ops) == 1
    assert ops[0].uid == "event-1"
    assert ops[0].op == "create"


def test_list_pending_ops_empty_for_unknown_account(engine) -> None:
    store = EventStore(engine)
    assert store.list_pending_ops("no-such-acc") == []


def test_delete_pending_op_removes_op(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    with Session(engine) as s:
        op = s.query(PendingOpRow).one()
        op_id = op.id

    store.delete_pending_op(op_id)

    with Session(engine) as s:
        assert s.query(PendingOpRow).count() == 0


def test_get_pending_op_returns_op(engine) -> None:
    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    with Session(engine) as s:
        op_id = s.query(PendingOpRow).one().id

    fetched = store.get_pending_op(op_id)
    assert fetched is not None
    assert fetched.id == op_id


def test_get_pending_op_nonexistent(engine) -> None:
    store = EventStore(engine)
    assert store.get_pending_op(99999) is None


# ── remove_event ──────────────────────────────────────────────────────────────


def test_remove_event_deletes_rows(engine) -> None:
    from lilical.models.event import EventInstanceRow

    store = EventStore(engine)
    store.queue_create(_event(rrule=None))

    with Session(engine) as s:
        assert s.query(EventRow).count() == 1
        assert s.query(EventInstanceRow).count() >= 1

    store.remove_event("event-1", "cal-1")

    with Session(engine) as s:
        assert s.query(EventRow).count() == 0
        assert s.query(EventInstanceRow).count() == 0


# ── list_accounts ─────────────────────────────────────────────────────────────


def test_list_accounts_returns_enabled(engine) -> None:
    store = EventStore(engine)
    accounts = store.list_accounts(enabled_only=True)
    assert len(accounts) == 1
    assert accounts[0].id == "acc-1"


def test_list_accounts_all_includes_disabled(engine) -> None:
    from sqlalchemy.orm import Session

    store = EventStore(engine)
    with Session(engine) as s, s.begin():
        acc = s.query(Account).filter(Account.id == "acc-1").one()
        acc.enabled = 0

    accounts = store.list_accounts(enabled_only=False)
    assert len(accounts) == 1
    assert accounts[0].enabled == 0


# ── set_account_orders ────────────────────────────────────────────────────────


def test_set_account_orders_updates_sort_order(engine) -> None:
    store = EventStore(engine)
    store.set_account_orders([("acc-1", 42)])

    acc = store.get_account("acc-1")
    assert acc.sort_order == 42


def test_set_account_orders_ignores_missing(engine) -> None:
    store = EventStore(engine)
    store.set_account_orders([("no-such", 100)])  # must not raise


# ── set_calendar_inclusion ────────────────────────────────────────────────────


def test_set_calendar_inclusion_toggles_flag(engine) -> None:
    store = EventStore(engine)
    cals_before = store.list_calendars("acc-1", included_only=False)
    assert cals_before[0].is_included == 1

    store.set_calendar_inclusion("cal-1", False)
    cals = store.list_calendars("acc-1", included_only=False)
    assert cals[0].is_included == 0

    store.set_calendar_inclusion("cal-1", True)
    cals = store.list_calendars("acc-1", included_only=False)
    assert cals[0].is_included == 1


def test_set_calendar_inclusion_emits_signal(engine) -> None:
    store = EventStore(engine)
    captured: list[str] = []
    store.cal_metadata_changed.connect(lambda cid: captured.append(cid))
    store.set_calendar_inclusion("cal-1", False)
    assert "cal-1" in captured


# ── set_calendar_orders ────────────────────────────────────────────────────────


def test_set_calendar_orders_updates_sort_order(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_orders([("cal-1", 10)])

    cal = store.get_calendar("cal-1")
    assert cal.sort_order == 10


def test_set_calendar_orders_ignores_missing(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_orders([("no-such", 5)])  # must not raise


# ── visible_calendar_ids ──────────────────────────────────────────────────────


def test_visible_calendar_ids_returns_included_and_visible(engine) -> None:
    store = EventStore(engine)
    ids = store.visible_calendar_ids()
    assert "cal-1" in ids


def test_visible_calendar_ids_excludes_hidden(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_visibility("cal-1", False)
    ids = store.visible_calendar_ids()
    assert "cal-1" not in ids


def test_visible_calendar_ids_excludes_not_included(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_inclusion("cal-1", False)
    ids = store.visible_calendar_ids()
    assert "cal-1" not in ids


# ── list_calendars included_only filter ────────────────────────────────────────


def test_list_calendars_excludes_not_included(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_inclusion("cal-1", False)
    cals = store.list_calendars("acc-1")
    assert len(cals) == 0


# ── set_calendar_color nonexistent ─────────────────────────────────────────────


def test_set_calendar_color_nonexistent_is_safe(engine) -> None:
    store = EventStore(engine)
    store.set_calendar_color("no-such", "#000000")  # must not raise


# ── queue_split_series / queue_truncate_series missing master ──────────────────


def test_queue_split_series_missing_master_raises_value_error(engine) -> None:
    store = EventStore(engine)
    split_at = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
    edited = _recurring_event(summary="New")
    with pytest.raises(ValueError, match="No master event"):
        store.queue_split_series("nonexistent", "cal-1", split_at, edited)


def test_queue_truncate_series_missing_master_raises_value_error(engine) -> None:
    store = EventStore(engine)
    until_dt = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="No master event"):
        store.queue_truncate_series("nonexistent", "cal-1", until_dt)


# ── CalDAV recurring-override regression (uq_events_provider) ──────────────────


def test_caldav_master_and_override_same_provider_event_id_both_persist(engine) -> None:
    """Master and recurrence override from the same .ics URL must not collide.

    CalDAV returns a single .ics resource that contains both the master VEVENT
    and one VEVENT per RECURRENCE-ID override. Both get the same provider_event_id
    (the .ics URL). The unique constraint must allow this because recurrence_id
    differs between them.
    """
    from lilical.backends.base import EventChange

    store = EventStore(engine)
    ics_url = "https://dav.example.com/cal/organizing-meeting.ics"

    master = Event(
        uid="3nhua1nmrlbjfu2hj0tktmrt1q@google.com",
        calendar_id="cal-1",
        provider_event_id=ics_url,
        dtstart=datetime(2024, 10, 21, 18, 0, tzinfo=timezone.utc),
        dtend=datetime(2024, 10, 21, 19, 0, tzinfo=timezone.utc),
        tz="America/Los_Angeles",
        summary="organizing meeting",
        rrule="FREQ=WEEKLY",
    )
    override = Event(
        uid="3nhua1nmrlbjfu2hj0tktmrt1q@google.com",
        calendar_id="cal-1",
        provider_event_id=ics_url,
        recurrence_id=datetime(2024, 10, 21, 18, 0, tzinfo=timezone.utc),
        dtstart=datetime(2024, 10, 24, 18, 0, tzinfo=timezone.utc),
        dtend=datetime(2024, 10, 24, 19, 0, tzinfo=timezone.utc),
        tz="America/Los_Angeles",
        summary="organizing meeting",
    )

    count = store.apply_remote_changes(
        "cal-1",
        [
            EventChange(kind="upsert", event=master, uid=master.uid),
            EventChange(kind="upsert", event=override, uid=override.uid),
        ],
        "{}",
    )

    assert count == 2
    with Session(engine) as s:
        rows = (
            s.query(EventRow)
            .filter_by(uid="3nhua1nmrlbjfu2hj0tktmrt1q@google.com", calendar_id="cal-1")
            .all()
        )
    assert len(rows) == 2
    recurrence_ids = {r.recurrence_id for r in rows}
    assert "" in recurrence_ids  # master
    assert any(r != "" for r in recurrence_ids)  # override
