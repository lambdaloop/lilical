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
                is_override INTEGER DEFAULT 0,
                recurrence_id TEXT NOT NULL DEFAULT ''
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
    from lilical.models.event import EventInstanceRow

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
            .filter_by(uid="series-upd", calendar_id="cal-1", recurrence_id=recurrence_id_dt.isoformat())
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
        assert master.local_dirty == 1
        exdates = json.loads(master.exdates)
        assert recurrence_id_dt.isoformat() in exdates

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
                dtstart_utc=int(datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc).timestamp()),
                dtend_utc=int(datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc).timestamp()),
                dtstart_local="2026-05-13T09:00:00+00:00",
                dtend_local="2026-05-13T09:30:00+00:00",
                recurrence_id="",
            )
        )
        s.add(
            EventInstanceRow(
                uid="series-gef",
                calendar_id="cal-1",
                dtstart_utc=int(datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc).timestamp()),
                dtend_utc=int(datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc).timestamp()),
                dtstart_local="2026-05-20T10:00:00+00:00",
                dtend_local="2026-05-20T10:30:00+00:00",
                is_override=1,
                recurrence_id=rid_iso,
            )
        )

    store = EventStore(engine)
    with Session(engine) as s:
        normal_inst = s.query(EventInstanceRow).filter_by(recurrence_id="").first()
        override_inst = s.query(EventInstanceRow).filter_by(recurrence_id=rid_iso).first()

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
