"""Tests for EventStore.create_subscription and delete_subscription."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lilical.backends.subscription import (
    SUBSCRIPTION_ACCOUNT_ID,
    SubscriptionCursor,
)
from lilical.models.account import Account
from lilical.models.calendar import Calendar
from lilical.models.event import Event, EventRow
from lilical.storage.event_store import EventStore


def _create_test_schema(engine) -> None:
    """Duplicates the schema setup used in event_store_test.py — kept inline
    here because the project doesn't make tests cross-importable."""
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


@pytest.fixture
def store() -> EventStore:
    engine = create_engine("sqlite:///:memory:")
    _create_test_schema(engine)
    return EventStore(engine)


def _event(uid: str, summary: str = "Evt") -> Event:
    return Event(
        uid=uid,
        calendar_id="",
        provider_event_id=uid,
        dtstart=datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
        summary=summary,
    )


def test_create_subscription_auto_creates_account(store: EventStore) -> None:
    store.create_subscription(
        canonical_source="https://example.com/a.ics",
        display_name="A",
        color="#5e9fff",
        events=[],
        content_sha256="sha-a",
    )
    acc = store.get_account(SUBSCRIPTION_ACCOUNT_ID)
    assert acc is not None
    assert acc.kind == "subscription"
    assert acc.display_name == "Subscriptions"


def test_create_subscription_reuses_existing_account(store: EventStore) -> None:
    store.create_subscription(
        canonical_source="https://example.com/a.ics",
        display_name="A",
        color="#5e9fff",
        events=[],
        content_sha256="sha-a",
    )
    store.create_subscription(
        canonical_source="https://example.com/b.ics",
        display_name="B",
        color="#aa00aa",
        events=[],
        content_sha256="sha-b",
    )
    with Session(store._engine) as s:
        accounts = (
            s.query(Account).filter(Account.id == SUBSCRIPTION_ACCOUNT_ID).all()
        )
    assert len(accounts) == 1


def test_create_subscription_inserts_calendar_with_reader_role(
    store: EventStore,
) -> None:
    cal_id = store.create_subscription(
        canonical_source="https://example.com/cal.ics",
        display_name="My Feed",
        color="#aabbcc",
        events=[],
        content_sha256="seed",
    )
    cal = store.get_calendar(cal_id)
    assert cal is not None
    assert cal.access_role == "reader"
    assert cal.provider_id == "https://example.com/cal.ics"
    assert cal.display_name == "My Feed"
    assert cal.color == "#aabbcc"
    assert cal.account_id == SUBSCRIPTION_ACCOUNT_ID


def test_create_subscription_persists_events(store: EventStore) -> None:
    events = [_event("a@x", "Alpha"), _event("b@x", "Beta")]
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=events,
        content_sha256="sha",
    )
    with Session(store._engine) as s:
        rows = s.query(EventRow).filter(EventRow.calendar_id == cal_id).all()
    uids = {r.uid for r in rows}
    assert uids == {"a@x", "b@x"}


def test_create_subscription_seeds_cursor_on_calendar_row(
    store: EventStore,
) -> None:
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=[],
        content_sha256="my-sha",
    )
    cal = store.get_calendar(cal_id)
    assert cal is not None
    assert cal.sync_cursor is not None
    restored = SubscriptionCursor.from_json(json.loads(cal.sync_cursor))
    assert restored.content_sha256 == "my-sha"


def test_delete_subscription_removes_calendar_and_events(
    store: EventStore,
) -> None:
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=[_event("e1"), _event("e2")],
        content_sha256="sha",
    )
    store.delete_subscription(cal_id)
    assert store.get_calendar(cal_id) is None
    with Session(store._engine) as s:
        remaining = s.query(EventRow).filter(EventRow.calendar_id == cal_id).all()
    assert remaining == []


def test_delete_subscription_removes_account_when_last(
    store: EventStore,
) -> None:
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=[],
        content_sha256="sha",
    )
    removed_account = store.delete_subscription(cal_id)
    assert removed_account is True
    assert store.get_account(SUBSCRIPTION_ACCOUNT_ID) is None


def test_delete_subscription_keeps_account_when_others_remain(
    store: EventStore,
) -> None:
    cal_a = store.create_subscription(
        canonical_source="file:///a.ics",
        display_name="A",
        color="#5e9fff",
        events=[],
        content_sha256="sha-a",
    )
    store.create_subscription(
        canonical_source="file:///b.ics",
        display_name="B",
        color="#5e9fff",
        events=[],
        content_sha256="sha-b",
    )
    removed_account = store.delete_subscription(cal_a)
    assert removed_account is False
    assert store.get_account(SUBSCRIPTION_ACCOUNT_ID) is not None


def test_delete_subscription_with_missing_id_silently_succeeds(
    store: EventStore,
) -> None:
    """Regression pin: delete_calendar is unconditional, so passing a bogus
    calendar_id is a no-op against the events table — but because the account
    sweep counts remaining calendars and prunes the account at 0, calling
    delete_subscription(missing_id) when an unrelated subscription exists
    leaves both intact (returns False)."""
    real_cal = store.create_subscription(
        canonical_source="file:///real.ics",
        display_name="Real",
        color="#5e9fff",
        events=[_event("keep")],
        content_sha256="sha",
    )
    removed = store.delete_subscription("does-not-exist")
    assert removed is False
    assert store.get_calendar(real_cal) is not None
    with Session(store._engine) as s:
        rows = s.query(EventRow).filter(EventRow.calendar_id == real_cal).all()
    assert {r.uid for r in rows} == {"keep"}


def test_delete_subscription_with_missing_id_wipes_account_when_empty(
    store: EventStore,
) -> None:
    """If the Subscriptions account has no calendars at all, even a bogus
    delete_subscription call will tear it down. Surprising — pinned so a
    future "raise on missing" refactor is a deliberate choice."""
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=[],
        content_sha256="sha",
    )
    # Tear down the only subscription so 0 calendars remain.
    store.delete_subscription(cal_id)
    # Re-create the account row by hand (no calendars), then delete with a
    # bogus id — account is wiped because the calendar count is 0.
    with store._write_session() as s:
        s.add(
            Account(
                id=SUBSCRIPTION_ACCOUNT_ID,
                kind="subscription",
                display_name="Subscriptions",
                identity="",
                server_url=None,
                secret_ref="",
                created_at=datetime.now(timezone.utc).isoformat(),
                enabled=1,
                include_contacts=0,
            )
        )
    removed = store.delete_subscription("anything")
    assert removed is True
    assert store.get_account(SUBSCRIPTION_ACCOUNT_ID) is None


def test_create_subscription_returns_calendar_id_uuid(store: EventStore) -> None:
    cal_id = store.create_subscription(
        canonical_source="file:///x.ics",
        display_name="X",
        color="#5e9fff",
        events=[],
        content_sha256="sha",
    )
    # uuid.uuid4 string form has dashes and length 36
    assert isinstance(cal_id, str)
    assert len(cal_id) == 36 and cal_id.count("-") == 4
    # And the calendar genuinely exists.
    assert store.get_calendar(cal_id) is not None


def test_two_subscriptions_share_one_account_two_calendars(
    store: EventStore,
) -> None:
    a = store.create_subscription(
        canonical_source="https://example.com/a.ics",
        display_name="A",
        color="#5e9fff",
        events=[_event("a1")],
        content_sha256="sha-a",
    )
    b = store.create_subscription(
        canonical_source="https://example.com/b.ics",
        display_name="B",
        color="#aa00aa",
        events=[_event("b1")],
        content_sha256="sha-b",
    )
    with Session(store._engine) as s:
        cals = (
            s.query(Calendar)
            .filter(Calendar.account_id == SUBSCRIPTION_ACCOUNT_ID)
            .all()
        )
    assert {c.id for c in cals} == {a, b}
