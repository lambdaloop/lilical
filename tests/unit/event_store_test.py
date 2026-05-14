from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lilical.backends.base import EventChange
from lilical.models.account import Account
from lilical.models.calendar import Calendar
from lilical.models.db import Base
from lilical.models.event import Event, EventRow
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
                enabled INTEGER DEFAULT 1
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
                rdates TEXT,
                attendees TEXT,
                categories TEXT,
                color TEXT,
                status TEXT DEFAULT 'CONFIRMED',
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
                UNIQUE(calendar_id, provider_event_id)
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
                is_override INTEGER DEFAULT 0
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


def test_model_metadata_creates_sqlite_schema() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)


def _event(**overrides) -> Event:
    data = {
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
        "attendees": ("anna@example.com",),
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
    assert json.loads(row.attendees) == ["anna@example.com"]
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
    assert event.attendees == ("anna@example.com",)
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
    cals_before = store.list_calendars("acc-1", visible_only=False)
    assert cals_before[0].is_visible == 1

    store.set_calendar_visibility("cal-1", False)
    cals = store.list_calendars("acc-1", visible_only=False)
    assert cals[0].is_visible == 0

    store.set_calendar_visibility("cal-1", True)
    cals = store.list_calendars("acc-1", visible_only=False)
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
