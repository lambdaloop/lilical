from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    PermanentError,
    SyncCursor,
    TransientError,
)
from lilical.backends.graph import (
    GraphBackend,
    GraphCursor,
    _graph_event_to_change,
    _graph_recurrence_to_rrule,
)


def _attach_mock(backend: GraphBackend, handler) -> None:
    """Wire a MockTransport into the backend's httpx client and stub out auth."""
    backend._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend._acquire_token = lambda: "fake-token"  # type: ignore[method-assign]


# -- mapping ------------------------------------------------------------------


def test_event_to_change_upsert() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-abc",
        "iCalUId": "uid-1@outlook.com",
        "subject": "Standup",
        "body": {"content": "weekly", "contentType": "text"},
        "location": {"displayName": "Zoom"},
        "@odata.etag": 'W/"abc"',
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.kind == "upsert"
    # Local uid is Graph's `id`, not iCalUId — calendarView/delta pre-expands
    # recurring events and every occurrence shares the same iCalUId, so using
    # `id` is what keeps occurrences from clobbering each other.
    assert change.uid == "AAMk-abc"
    assert change.event is not None
    assert change.event.summary == "Standup"
    assert change.event.description == "weekly"
    assert change.event.location == "Zoom"
    assert change.event.provider_event_id == "AAMk-abc"
    assert change.event.etag == 'W/"abc"'


def test_event_to_change_removed_marker() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-xyz",
        "iCalUId": "uid-2@outlook.com",
        "@removed": {"reason": "deleted"},
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.kind == "delete"
    assert change.uid == "AAMk-xyz"


def test_event_to_change_falls_back_to_id_for_uid() -> None:
    data: dict[str, Any] = {"id": "AAMk-foo", "subject": "x"}
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.uid == "AAMk-foo"


# -- delta pagination ---------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_sync_paginates_and_emits_delta_link() -> None:
    page1 = {
        "value": [
            {"id": "e1", "iCalUId": "u1", "subject": "A"},
            {"id": "e2", "iCalUId": "u2", "subject": "B"},
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$skiptoken=PAGE2",
    }
    page2 = {
        "value": [{"id": "e3", "iCalUId": "u3", "subject": "C"}],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=ABC",
    }
    pages = [page1, page2]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0))

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    backend._account_emails = frozenset()  # skip /me fetch in tests
    _attach_mock(backend, handler)

    collected = []
    last_cursor: SyncCursor | None = None
    async for batch, cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)
        last_cursor = cursor

    # Graph local uid comes from `id`, not `iCalUId`.
    assert [c.uid for c in collected] == ["e1", "e2", "e3"]
    assert isinstance(last_cursor, GraphCursor)
    assert last_cursor.delta_link and "deltatoken=ABC" in last_cursor.delta_link


@pytest.mark.asyncio
async def test_incremental_sync_uses_delta_link() -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "e9", "iCalUId": "u9", "subject": "Updated"},
                    {"id": "e10", "iCalUId": "u10", "@removed": {"reason": "deleted"}},
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=NEW",
            },
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    backend._account_emails = frozenset()  # skip /me fetch in tests
    _attach_mock(backend, handler)

    cursor = GraphCursor(
        delta_link="https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=OLD"
    )
    changes, new_cursor = await backend.incremental_sync("cal-1", cursor)

    assert captured == [cursor.delta_link]
    assert [c.kind for c in changes] == ["upsert", "delete"]
    assert new_cursor.delta_link is not None
    assert "deltatoken=NEW" in new_cursor.delta_link


@pytest.mark.asyncio
async def test_incremental_sync_without_delta_link_raises_cursor_expired() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(500))

    with pytest.raises(CursorExpired):
        await backend.incremental_sync("cal-1", GraphCursor(delta_link=None))


# -- error classification -----------------------------------------------------


@pytest.mark.asyncio
async def test_401_maps_to_auth_expired() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(
        backend, lambda req: httpx.Response(401, json={"error": "unauthorized"})
    )
    with pytest.raises(AuthExpired):
        await backend.list_calendars()


@pytest.mark.asyncio
async def test_410_maps_to_cursor_expired() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(410, json={"error": "gone"}))
    with pytest.raises(CursorExpired):
        async for _ in backend.initial_sync("cal-1"):
            pass


@pytest.mark.asyncio
async def test_412_maps_to_conflict() -> None:
    from lilical.models.event import Event

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(
        backend, lambda req: httpx.Response(412, json={"error": "etag mismatch"})
    )
    evt = Event(
        uid="u1", calendar_id="cal-1", provider_event_id="AAMk-abc", summary="x"
    )
    with pytest.raises(ConflictError):
        await backend.update_event("cal-1", evt, if_match='W/"old"')


@pytest.mark.asyncio
async def test_429_maps_to_transient() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(429, json={"error": "throttled"}))
    with pytest.raises(TransientError):
        await backend.list_calendars()


@pytest.mark.asyncio
async def test_500_maps_to_transient() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(503, json={"error": "down"}))
    with pytest.raises(TransientError):
        await backend.list_calendars()


@pytest.mark.asyncio
async def test_404_maps_to_permanent() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(PermanentError):
        await backend.list_calendars()


# -- cursor round-trip --------------------------------------------------------


def test_graph_cursor_roundtrip() -> None:
    c = GraphCursor(delta_link="https://example/delta?token=X")
    j = c.to_json()
    assert j == {"_type": "graph", "delta_link": "https://example/delta?token=X"}
    c2 = GraphCursor.from_json(json.loads(json.dumps(j)))
    assert c2.delta_link == c.delta_link


# -- list_calendars happy path -----------------------------------------------


@pytest.mark.asyncio
async def test_list_calendars_shape() -> None:
    body = {
        "value": [
            {"id": "AAA", "name": "Work"},
            {"id": "BBB", "name": "Personal"},
        ]
    }
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, lambda req: httpx.Response(200, json=body))

    cals = await backend.list_calendars()
    # color is None here because the mocked response has no hexColor/color fields.
    assert cals == [
        {"id": "AAA", "display_name": "Work", "provider_id": "AAA", "color": None},
        {"id": "BBB", "display_name": "Personal", "provider_id": "BBB", "color": None},
    ]


# -- _graph_event_to_change: real Graph-shaped payloads ----------------------


def test_event_to_change_extracts_timed_event() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-timed",
        "iCalUId": "uid-timed@outlook.com",
        "subject": "Review",
        "start": {
            "dateTime": "2026-05-13T09:00:00.0000000",
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": "2026-05-13T10:00:00.0000000",
            "timeZone": "America/New_York",
        },
        "isAllDay": False,
        "showAs": "busy",
        "isCancelled": False,
        "webLink": "https://outlook.office365.com/...",
        "lastModifiedDateTime": "2026-05-12T08:00:00.1234567Z",
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.kind == "upsert"
    e = change.event
    assert e is not None
    assert e.dtstart is not None and e.dtstart.tzinfo is not None
    # 09:00 in NY (May → EDT, UTC-4) → 13:00 UTC
    assert e.dtstart.astimezone(timezone.utc) == datetime(
        2026, 5, 13, 13, 0, tzinfo=timezone.utc
    )
    assert e.dtend is not None
    assert e.dtend.astimezone(timezone.utc) == datetime(
        2026, 5, 13, 14, 0, tzinfo=timezone.utc
    )
    assert e.tz == "America/New_York"
    assert e.all_day is False
    assert e.transparency == "OPAQUE"
    assert e.status == "CONFIRMED"
    assert e.url and "outlook" in e.url
    assert e.last_modified is not None


def test_event_to_change_handles_all_day_event() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-allday",
        "iCalUId": "uid-allday@outlook.com",
        "subject": "Holiday",
        "start": {"dateTime": "2026-07-04T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-07-05T00:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": True,
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    e = change.event
    assert e is not None
    assert e.all_day is True
    # All-day events are re-anchored to local zone midnight so .date() returns
    # the right calendar day for users west of UTC.
    from datetime import date

    assert e.dtstart is not None
    assert e.dtstart.tzinfo is not None
    assert e.dtstart.date() == date(2026, 7, 4)
    assert e.dtend is not None
    assert e.dtend.date() == date(2026, 7, 5)


def test_event_to_change_marks_cancelled() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-cancel",
        "subject": "Killed meeting",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "isCancelled": True,
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.status == "CANCELLED"


def test_event_to_change_show_as_free_is_transparent() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-free",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "showAs": "free",
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.transparency == "TRANSPARENT"


def test_event_to_change_extracts_categories_and_attendees() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-rich",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "categories": ["Work", "Important"],
        "attendees": [
            {"emailAddress": {"address": "alice@example.com", "name": "Alice"}},
            {"emailAddress": {"address": "bob@example.com", "name": "Bob"}},
        ],
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert set(change.event.categories) == {"Work", "Important"}
    assert {a.email for a in change.event.attendees} == {
        "alice@example.com",
        "bob@example.com",
    }


def test_event_to_change_occurrences_return_none() -> None:
    """Pre-expanded occurrence rows are dropped at the parser boundary.
    The seriesMaster's rrule drives instance generation via the expander."""
    occurrences: list[dict[str, Any]] = [
        {
            "id": f"AAMk-occ-{i}",
            "iCalUId": "uid-shared@outlook.com",
            "subject": "Weekly standup",
            "type": "occurrence",
            "start": {
                "dateTime": f"2026-05-{13 + i:02d}T09:00:00.0000000",
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": f"2026-05-{13 + i:02d}T09:30:00.0000000",
                "timeZone": "UTC",
            },
        }
        for i in range(3)
    ]
    for occ in occurrences:
        assert _graph_event_to_change(occ, "cal-1") is None


def test_event_to_change_series_master_returns_event_with_rrule() -> None:
    """seriesMaster is kept and its recurrence pattern decoded into an rrule.
    The expander uses this rrule to generate instance rows."""
    data: dict[str, Any] = {
        "id": "AAMk-master",
        "iCalUId": "uid-master@outlook.com",
        "subject": "Weekly standup",
        "type": "seriesMaster",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {
                "type": "weekly",
                "interval": 1,
                "daysOfWeek": ["wednesday"],
            },
            "range": {
                "type": "noEnd",
                "startDate": "2026-05-13",
            },
        },
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.rrule is not None
    assert "FREQ=WEEKLY" in change.event.rrule
    assert change.event.summary == "Weekly standup"


def test_event_to_change_single_instance_no_rrule() -> None:
    data: dict[str, Any] = {
        "id": "AAMk-single",
        "subject": "One-off",
        "type": "singleInstance",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.rrule is None


def test_series_master_is_kept_occurrences_are_dropped() -> None:
    """A batch containing a seriesMaster alongside its occurrences should
    produce exactly one EventChange for the master (with rrule); occurrences
    are dropped — the expander generates instance rows from the master's rrule."""
    master: dict[str, Any] = {
        "id": "AAMk-master",
        "type": "seriesMaster",
        "subject": "Weekly standup",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["tuesday"]},
            "range": {"type": "noEnd", "startDate": "2026-05-13"},
        },
    }
    occurrences: list[dict[str, Any]] = [
        {
            "id": f"AAMk-occ-{i}",
            "iCalUId": "uid-shared@outlook.com",
            "type": "occurrence",
            "seriesMasterId": "AAMk-master",
            "subject": "Weekly standup",
            "start": {
                "dateTime": f"2026-05-{13 + 7 * i:02d}T09:00:00.0000000",
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": f"2026-05-{13 + 7 * i:02d}T09:30:00.0000000",
                "timeZone": "UTC",
            },
        }
        for i in range(3)
    ]
    batch = [
        c
        for c in (_graph_event_to_change(ev, "cal-1") for ev in [master, *occurrences])
        if c is not None
    ]
    # Only the seriesMaster produces an EventChange; 3 occurrences return None.
    assert len(batch) == 1
    assert batch[0].event is not None
    assert batch[0].event.rrule is not None
    assert "FREQ=WEEKLY" in batch[0].event.rrule


# -- end-to-end: parser → EventStore → event_instances expansion --------------


def test_parsed_graph_event_creates_instance_row(tmp_path) -> None:
    """Regression for the blank-UI bug: prove a Graph timed event flows all
    the way to an EventInstanceRow. Before the parser fix, dtstart was empty,
    so _rebuild_instances_for short-circuited and the views had nothing."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from lilical.models.account import Account
    from lilical.models.calendar import Calendar
    from lilical.models.db import Base
    from lilical.models.event import EventInstanceRow
    from lilical.storage.event_store import EventStore

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            Account(
                id="acc-1",
                kind="graph",
                display_name="O",
                identity="u@example.com",
                secret_ref="acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="graph-cal-id",
                display_name="Calendar",
                color="#000000",
                access_role="owner",
            )
        )

    data: dict[str, Any] = {
        "id": "AAMk-pipeline",
        "iCalUId": "uid-pipeline@outlook.com",
        "subject": "Pipeline test",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
    }
    change = _graph_event_to_change(data, "cal-1")

    store = EventStore(engine)
    n = store.apply_remote_changes(
        "cal-1",
        [change],
        '{"delta_link": null}',
    )
    assert n == 1
    with Session(engine) as session:
        instances = session.query(EventInstanceRow).all()
    assert len(instances) == 1
    assert instances[0].uid == "AAMk-pipeline"
    assert instances[0].dtstart_utc == int(
        datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc).timestamp()
    )


def test_series_master_drives_instance_rows_via_rrule(tmp_path) -> None:
    """With the new approach, only the seriesMaster produces an EventChange.
    apply_remote_changes stores the master with its rrule and the expander
    generates EventInstanceRows. Occurrences from the API are dropped."""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from lilical.models.account import Account
    from lilical.models.calendar import Calendar
    from lilical.models.db import Base
    from lilical.models.event import EventInstanceRow
    from lilical.storage.event_store import EventStore

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            Account(
                id="acc-1",
                kind="graph",
                display_name="O",
                identity="u@example.com",
                secret_ref="acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="graph-cal-id",
                display_name="Calendar",
                color="#000000",
                access_role="owner",
            )
        )

    master_json: dict[str, Any] = {
        "id": "AAMk-master",
        "iCalUId": "uid-shared@outlook.com",
        "type": "seriesMaster",
        "subject": "Weekly standup",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["tuesday"]},
            "range": {
                "type": "numbered",
                "startDate": "2026-05-13",
                "numberOfOccurrences": 3,
            },
        },
    }
    occurrence_jsons: list[dict[str, Any]] = [
        {
            "id": f"AAMk-occ-{i}",
            "iCalUId": "uid-shared@outlook.com",
            "type": "occurrence",
            "seriesMasterId": "AAMk-master",
            "subject": "Weekly standup",
            "start": {
                "dateTime": f"2026-05-{13 + 7 * i:02d}T09:00:00.0000000",
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": f"2026-05-{13 + 7 * i:02d}T09:30:00.0000000",
                "timeZone": "UTC",
            },
        }
        for i in range(3)
    ]

    all_json = [master_json, *occurrence_jsons]
    changes = [
        c
        for c in (_graph_event_to_change(ev, "cal-1") for ev in all_json)
        if c is not None
    ]
    # Only the master produces a change; 3 occurrences return None.
    assert len(changes) == 1
    assert changes[0].event is not None
    assert changes[0].event.rrule is not None

    store = EventStore(engine)
    store.apply_remote_changes(
        "cal-1", changes, '{"_type": "graph", "delta_link": null}'
    )

    # The expander should produce EventInstanceRows from rrule expansion.
    with Session(engine) as session:
        instances = session.query(EventInstanceRow).all()
    assert len(instances) >= 1, (
        f"Expected rrule expansion to produce instances, got {len(instances)}"
    )
    # All instances share the master's uid.
    master_uid = changes[0].event.uid
    assert all(inst.uid == master_uid for inst in instances)


# -- _graph_recurrence_to_rrule: pattern.type axis ---------------------------


def test_recurrence_to_rrule_daily() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=DAILY"


def test_recurrence_to_rrule_daily_with_interval() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "daily", "interval": 3},
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=DAILY;INTERVAL=3"


def test_recurrence_to_rrule_weekly_multi_day() -> None:
    rec: dict[str, Any] = {
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday", "wednesday", "friday"],
        },
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_recurrence_to_rrule_absolute_monthly() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 15},
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=MONTHLY;BYMONTHDAY=15"


def test_recurrence_to_rrule_relative_monthly_second_tuesday() -> None:
    rec: dict[str, Any] = {
        "pattern": {
            "type": "relativeMonthly",
            "interval": 1,
            "index": "second",
            "daysOfWeek": ["tuesday"],
        },
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=MONTHLY;BYDAY=2TU"


def test_recurrence_to_rrule_relative_monthly_last_friday() -> None:
    rec: dict[str, Any] = {
        "pattern": {
            "type": "relativeMonthly",
            "interval": 1,
            "index": "last",
            "daysOfWeek": ["friday"],
        },
        "range": {"type": "noEnd", "startDate": "2026-05-13"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=MONTHLY;BYDAY=-1FR"


def test_recurrence_to_rrule_absolute_yearly() -> None:
    rec: dict[str, Any] = {
        "pattern": {
            "type": "absoluteYearly",
            "interval": 1,
            "month": 7,
            "dayOfMonth": 4,
        },
        "range": {"type": "noEnd", "startDate": "2026-07-04"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=YEARLY;BYMONTH=7;BYMONTHDAY=4"


def test_recurrence_to_rrule_relative_yearly() -> None:
    rec: dict[str, Any] = {
        "pattern": {
            "type": "relativeYearly",
            "interval": 1,
            "month": 11,
            "index": "fourth",
            "daysOfWeek": ["thursday"],
        },
        "range": {"type": "noEnd", "startDate": "2026-11-26"},
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"


# -- _graph_recurrence_to_rrule: range.type axis -----------------------------


def test_recurrence_to_rrule_numbered_range() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {
            "type": "numbered",
            "startDate": "2026-05-13",
            "numberOfOccurrences": 10,
        },
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=DAILY;COUNT=10"


def test_recurrence_to_rrule_end_date_range() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {
            "type": "endDate",
            "startDate": "2026-05-13",
            "endDate": "2026-06-13",
        },
    }
    assert _graph_recurrence_to_rrule(rec) == "FREQ=DAILY;UNTIL=20260613T235959Z"


def test_recurrence_to_rrule_returns_none_for_unknown_pattern() -> None:
    rec: dict[str, Any] = {
        "pattern": {"type": "alienCycle", "interval": 1},
        "range": {"type": "noEnd"},
    }
    assert _graph_recurrence_to_rrule(rec) is None


def test_recurrence_to_rrule_returns_none_for_missing_subobjects() -> None:
    assert _graph_recurrence_to_rrule(None) is None
    assert _graph_recurrence_to_rrule({}) is None
    assert _graph_recurrence_to_rrule({"pattern": {"type": "daily"}}) is None


# -- master hydration --------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_delta_hydrates_exceptions_from_master() -> None:
    """calendarView/delta returns exception overrides with empty subject/body —
    the real values live on the seriesMaster. `_drain_delta` should fetch each
    unique master once and merge its fields into the exception event JSON.
    Plain occurrences are dropped and no longer trigger hydration requests."""
    delta_body = {
        "value": [
            {
                # Plain occurrence — dropped, should NOT trigger a $batch request.
                "id": "AAMk-occ-1",
                "iCalUId": "uid-shared@outlook.com",
                "subject": None,
                "body": {"contentType": "html", "content": ""},
                "location": {"displayName": ""},
                "type": "occurrence",
                "seriesMasterId": "AAMk-master-1",
                "start": {
                    "dateTime": "2026-05-13T09:00:00.0000000",
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": "2026-05-13T09:30:00.0000000",
                    "timeZone": "UTC",
                },
            },
            {
                # Exception override with no subject — should trigger hydration.
                "id": "AAMk-exc-1",
                "iCalUId": "uid-shared@outlook.com",
                "subject": "",
                "body": {"contentType": "html", "content": ""},
                "location": {"displayName": ""},
                "type": "exception",
                "seriesMasterId": "AAMk-master-1",
                "originalStart": {
                    "dateTime": "2026-05-20T09:00:00.0000000",
                    "timeZone": "UTC",
                },
                "start": {
                    "dateTime": "2026-05-20T10:00:00.0000000",
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": "2026-05-20T10:30:00.0000000",
                    "timeZone": "UTC",
                },
            },
            {
                "id": "AAMk-single",
                "iCalUId": "uid-single@outlook.com",
                "subject": "One-off",
                "type": "singleInstance",
                "start": {
                    "dateTime": "2026-06-01T09:00:00.0000000",
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": "2026-06-01T09:30:00.0000000",
                    "timeZone": "UTC",
                },
            },
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=END",
    }
    master_body = {
        "id": "AAMk-master-1",
        "subject": "Weekly standup",
        "body": {"contentType": "text", "content": "team standup"},
        "location": {"displayName": "Zoom"},
        "type": "seriesMaster",
    }

    import json as _json

    batch_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            body = _json.loads(req.content)
            batch_calls.append(body)
            responses = [
                {"id": r["id"], "status": 200, "body": master_body}
                for r in body["requests"]
                if r["id"] == "AAMk-master-1"
            ]
            return httpx.Response(200, json={"responses": responses})
        return httpx.Response(200, json=delta_body)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    collected = []
    async for batch, _cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)

    # One $batch call for the shared master (occurrence + exception both reference
    # it but dedup ensures a single fetch).
    assert len(batch_calls) == 1
    assert len(batch_calls[0]["requests"]) == 1

    # collected has: synthesized seriesMaster + exception override + singleInstance.
    # Occurrence is dropped.
    assert len(collected) == 3
    overrides = [c for c in collected if c.event.recurrence_id is not None]
    singles = [c for c in collected if c.uid == "AAMk-single"]
    assert len(overrides) == 1
    assert len(singles) == 1
    # The exception override inherited subject from master and has recurrence_id.
    assert overrides[0].event.summary == "Weekly standup"
    assert overrides[0].event.description == "team standup"
    assert overrides[0].event.location == "Zoom"
    # Single instance with its own subject is untouched.
    assert singles[0].event.summary == "One-off"


@pytest.mark.asyncio
async def test_drain_delta_fetches_master_for_occurrence_regardless_of_subject() -> (
    None
):
    """Occurrences always trigger a master fetch for synthesis, even when the
    occurrence already has its own subject. Without the master we'd have no rrule."""
    delta_body = {
        "value": [
            {
                "id": "AAMk-occ-1",
                "subject": "Custom title for this week",
                "type": "occurrence",
                "seriesMasterId": "AAMk-master-1",
                "start": {
                    "dateTime": "2026-05-13T09:00:00.0000000",
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": "2026-05-13T09:30:00.0000000",
                    "timeZone": "UTC",
                },
            },
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=END",
    }

    batch_calls: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            batch_calls.append(req)
            return httpx.Response(200, json={"responses": []})
        return httpx.Response(200, json=delta_body)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    async for _batch, _cursor in backend.initial_sync("cal-1"):
        pass

    # Master fetch is attempted even though the occurrence has its own subject.
    assert len(batch_calls) == 1


@pytest.mark.asyncio
async def test_drain_delta_cross_page_master_cache() -> None:
    """The same seriesMasterId appearing on two delta pages (as exceptions)
    should trigger exactly one $batch call total, not one per page."""
    import json as _json

    exc = {
        "iCalUId": "uid-shared@outlook.com",
        "subject": "",
        "type": "exception",
        "seriesMasterId": "AAMk-master-X",
        "originalStart": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "start": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:30:00.0000000", "timeZone": "UTC"},
    }
    page1 = {
        "value": [{**exc, "id": "AAMk-exc-p1"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
    }
    page2 = {
        "value": [
            {
                **exc,
                "id": "AAMk-exc-p2",
                "originalStart": {
                    "dateTime": "2026-05-20T09:00:00.0000000",
                    "timeZone": "UTC",
                },
            }
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=END",
    }
    master = {
        "id": "AAMk-master-X",
        "subject": "Weekly",
        "body": {"contentType": "text", "content": ""},
        "location": {"displayName": ""},
    }

    batch_calls: list[dict] = []
    urls_seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        urls_seen.append(url)
        if "/$batch" in url:
            body = _json.loads(req.content)
            batch_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "responses": [
                        {"id": r["id"], "status": 200, "body": master}
                        for r in body["requests"]
                    ]
                },
            )
        if "page2" in url:
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    async for _batch, _cursor in backend.initial_sync("cal-1"):
        pass

    # $batch called once (page 1 exception triggers hydration), page 2 reuses
    # the cache — no second $batch call for the same master.
    assert len(batch_calls) == 1


@pytest.mark.asyncio
async def test_graph_batch_get_chunks_into_groups_of_20() -> None:
    """25 master IDs → two $batch POSTs (20 + 5)."""
    import json as _json

    batch_request_sizes: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            body = _json.loads(req.content)
            batch_request_sizes.append(len(body["requests"]))
            return httpx.Response(200, json={"responses": []})
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
            },
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    ids = [f"master-{i}" for i in range(25)]
    await backend._graph_batch_get(ids)

    assert batch_request_sizes == [20, 5]


@pytest.mark.asyncio
async def test_initial_sync_uses_calendarview_delta_endpoint() -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendars/cal-1/calendarView/delta?$deltatoken=X",
            },
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    async for _batch, _cursor in backend.initial_sync("cal-1"):
        pass

    assert captured, "expected at least one request"
    assert "/calendarView/delta" in captured[0]
    assert "startDateTime=" in captured[0]
    assert "endDateTime=" in captured[0]


def test_series_master_creates_instance_rows_via_rrule(tmp_path) -> None:
    """seriesMaster is kept; its rrule drives EventInstanceRow creation.
    Full end-to-end coverage is in test_series_master_drives_instance_rows_via_rrule."""
    data: dict[str, Any] = {
        "id": "AAMk-series",
        "iCalUId": "uid-weekly@outlook.com",
        "type": "seriesMaster",
        "subject": "Weekly standup",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
            "range": {
                "type": "numbered",
                "startDate": "2026-05-13",
                "numberOfOccurrences": 4,
            },
        },
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.rrule is not None
    assert "FREQ=WEEKLY" in change.event.rrule
    assert "COUNT=4" in change.event.rrule


# ── write path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_event_posts_with_correct_body() -> None:
    from lilical.models.event import Event

    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "id": "AAMk-new",
                "iCalUId": "uid-new@outlook.com",
                "@odata.etag": 'W/"etag-new"',
            },
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, _handler)

    event = Event(
        uid="uid-new@outlook.com",
        calendar_id="cal-A",
        summary="New event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )
    result = await backend.create_event("cal-A", event)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert "/me/calendars/cal-A/events" in str(requests[0].url)
    assert result.provider_event_id == "AAMk-new"
    assert result.etag == 'W/"etag-new"'


@pytest.mark.asyncio
async def test_update_event_uses_ifmatch_header() -> None:
    from lilical.models.event import Event

    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "AAMk-1", "@odata.etag": 'W/"new-etag"'},
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, _handler)

    event = Event(
        uid="u1",
        calendar_id="cal-A",
        provider_event_id="AAMk-1",
        summary="Updated event",
    )
    await backend.update_event("cal-A", event, if_match='W/"old-etag"')

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].headers.get("If-Match") == 'W/"old-etag"'
    assert "/me/events/AAMk-1" in str(requests[0].url)


@pytest.mark.asyncio
async def test_delete_event_uses_ifmatch_header() -> None:
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, _handler)

    await backend.delete_event("cal-A", "AAMk-del", if_match='W/"etag"')

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].headers.get("If-Match") == 'W/"etag"'
    assert "/me/events/AAMk-del" in str(requests[0].url)


@pytest.mark.asyncio
async def test_aclose_closes_and_clears_http_client() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    # Force client creation.
    _attach_mock(backend, lambda r: httpx.Response(200, json={}))
    assert backend._http is not None

    await backend.aclose()

    assert backend._http is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    # No HTTP client created yet.
    assert backend._http is None
    await backend.aclose()  # must not raise


# ── recurring: exception override read path ───────────────────────────────────


def test_event_to_change_exception_returns_override_event() -> None:
    """exception-type Graph events produce an override Event: uid is the master's
    id, recurrence_id is the original start of the cancelled occurrence, rrule=None."""
    data: dict[str, Any] = {
        "id": "AAMk-exc",
        "iCalUId": "uid-series@outlook.com",
        "subject": "Standup (moved)",
        "type": "exception",
        "seriesMasterId": "AAMk-master",
        "originalStart": {
            "dateTime": "2026-05-20T09:00:00.0000000",
            "timeZone": "UTC",
        },
        "start": {
            "dateTime": "2026-05-20T10:00:00.0000000",
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": "2026-05-20T10:30:00.0000000",
            "timeZone": "UTC",
        },
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is not None
    ev = change.event
    assert ev is not None
    assert ev.uid == "AAMk-master"
    assert ev.recurrence_id is not None
    assert ev.recurrence_id == datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    assert ev.rrule is None
    assert ev.summary == "Standup (moved)"


def test_event_to_change_exception_without_original_start_returns_none() -> None:
    """Attendee-view exceptions lack originalStart; they must be dropped rather than
    overwriting the seriesMaster row (which has recurrence_id='')."""
    data: dict[str, Any] = {
        "id": "AAMk-exc-attendee",
        "subject": "Standup",
        "type": "exception",
        "seriesMasterId": "AAMk-master",
        # originalStart intentionally absent — attendee view from Graph
        "start": {
            "dateTime": "2026-05-20T15:00:00.0000000",
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": "2026-05-20T15:30:00.0000000",
            "timeZone": "UTC",
        },
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change is None


# ── recurring: event-to-Graph-JSON write path ─────────────────────────────────


def test_event_to_graph_json_emits_recurrence_for_rrule() -> None:
    """_event_to_graph_json must include a recurrence block for events with rrule."""
    from lilical.backends.graph import _event_to_graph_json
    from lilical.models.event import Event as _Event

    event = _Event(
        uid="uid-w@outlook.com",
        calendar_id="cal-1",
        summary="Weekly standup",
        rrule="FREQ=WEEKLY;BYDAY=MO,WE",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
    )
    body = _event_to_graph_json(event)
    assert "recurrence" in body
    rec = body["recurrence"]
    assert rec["pattern"]["type"] == "weekly"
    assert "monday" in rec["pattern"]["daysOfWeek"]
    assert "wednesday" in rec["pattern"]["daysOfWeek"]
    assert rec["range"]["type"] == "noEnd"


def test_event_to_graph_json_omits_recurrence_for_non_recurring() -> None:
    """Non-recurring events must not include a recurrence key."""
    from lilical.backends.graph import _event_to_graph_json
    from lilical.models.event import Event as _Event

    event = _Event(
        uid="uid-s@outlook.com",
        calendar_id="cal-1",
        summary="One-off",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    body = _event_to_graph_json(event)
    assert "recurrence" not in body


def test_event_to_graph_json_includes_categories() -> None:
    """categories must appear in the serialized Graph JSON body."""
    from lilical.backends.graph import _event_to_graph_json
    from lilical.models.event import Event as _Event

    event = _Event(
        uid="u@o.c",
        calendar_id="cal-1",
        summary="Tagged event",
        categories=("Work", "Important"),
    )
    body = _event_to_graph_json(event)
    assert "categories" in body
    assert set(body["categories"]) == {"Work", "Important"}


def test_event_to_graph_json_includes_attendees_as_email_address_objects() -> None:
    """Attendees stored as dicts (email/name) must appear in the Graph body
    under the emailAddress sub-object format."""
    from lilical.backends.graph import _event_to_graph_json
    from lilical.models.event import Attendee as _Attendee
    from lilical.models.event import Event as _Event

    event = _Event(
        uid="u@o.c",
        calendar_id="cal-1",
        summary="Team meeting",
        attendees=(
            _Attendee(email="alice@example.com", display_name="Alice"),
            _Attendee(email="bob@example.com", display_name="Bob"),
        ),
    )
    body = _event_to_graph_json(event)
    assert "attendees" in body
    addresses = {a["emailAddress"]["address"] for a in body["attendees"]}
    assert addresses == {"alice@example.com", "bob@example.com"}


@pytest.mark.asyncio
async def test_update_instance_matches_occurrence_in_non_utc_timezone() -> None:
    """update_instance must find the correct instance even when Graph returns
    start.timeZone in a non-UTC zone — the pre-fix code did a naive comparison
    that caused false 404s for non-UTC accounts (e.g. America/New_York)."""
    from lilical.models.event import Event as _Event

    # 09:00 UTC = 05:00 EDT (America/New_York, UTC-4 in May)
    recurrence_id_dt = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)

    instances_body = {
        "value": [
            {
                "id": "AAMk-occ-ny",
                "start": {
                    "dateTime": "2026-05-20T05:00:00.0000000",
                    "timeZone": "America/New_York",
                },
            }
        ]
    }
    patch_requests: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "instances" in url:
            return httpx.Response(200, json=instances_body)
        if req.method == "PATCH":
            patch_requests.append(url)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "not found"})

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    event = _Event(
        uid="u-master@outlook.com",
        calendar_id="cal-1",
        summary="Updated standup",
        dtstart=recurrence_id_dt,
        dtend=datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
    )
    # Must not raise PermanentError — the timezone mismatch was the bug being fixed.
    await backend.update_instance("cal-1", "AAMk-master", recurrence_id_dt, event)

    assert len(patch_requests) == 1, (
        "Expected one PATCH request for the matched occurrence"
    )
    assert "AAMk-occ-ny" in patch_requests[0]


@pytest.mark.asyncio
async def test_create_event_returns_uid_matching_delta() -> None:
    """create_event returns uid=data['id'] so it matches _graph_event_to_change."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "AAMk-new",
                "iCalUId": "uid-new@outlook.com",
                "@odata.etag": 'W/"etag"',
            },
        )

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, _handler)

    from lilical.models.event import Event

    event = Event(
        uid="local-uuid",
        calendar_id="cal-A",
        summary="Test",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )
    result = await backend.create_event("cal-A", event)

    # Must use id, not iCalUId — otherwise mark_synced writes the wrong uid
    # and the next delta can't find the row by (uid, calendar_id).
    assert result.uid == "AAMk-new", (
        f"Expected uid=AAMk-new (the Graph id), got {result.uid!r}"
    )
    assert result.provider_event_id == "AAMk-new"


# ── seriesMaster synthesis from occurrences ───────────────────────────────────


@pytest.mark.asyncio
async def test_drain_delta_synthesizes_master_for_unknown_occurrence() -> None:
    """When a delta page contains an occurrence whose seriesMaster isn't in the
    page, the backend should fetch the master via $batch and emit it as a
    seriesMaster EventChange so the rrule is captured in the DB."""
    import json as _json

    delta_body = {
        "value": [
            {
                "id": "AAMk-occ-wed",
                "iCalUId": "uid-weekly-wed@outlook.com",
                "subject": "Katie / Lili",
                "type": "occurrence",
                "seriesMasterId": "AAMk-master-wed",
                "start": {"dateTime": "2026-05-13T13:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-13T14:00:00.0000000", "timeZone": "UTC"},
            }
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
    }
    master_body = {
        "id": "AAMk-master-wed",
        "iCalUId": "uid-weekly-wed@outlook.com",
        "subject": "Katie / Lili",
        "type": "seriesMaster",
        "start": {"dateTime": "2024-01-03T13:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2024-01-03T14:00:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
            "range": {"type": "noEnd", "startDate": "2024-01-03"},
        },
    }

    batch_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            body = _json.loads(req.content)
            batch_calls.append(body)
            responses = [
                {"id": r["id"], "status": 200, "body": master_body}
                for r in body["requests"]
                if r["id"] == "AAMk-master-wed"
            ]
            return httpx.Response(200, json={"responses": responses})
        return httpx.Response(200, json=delta_body)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    collected = []
    async for batch, _cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)

    # The occurrence was dropped; the synthesized seriesMaster was emitted.
    assert len(batch_calls) == 1
    assert len(collected) == 1
    change = collected[0]
    assert change.uid == "AAMk-master-wed"
    assert change.event.rrule is not None
    assert "FREQ=WEEKLY" in change.event.rrule
    assert "BYDAY=WE" in change.event.rrule


@pytest.mark.asyncio
async def test_drain_delta_synthesized_master_dedup_across_pages() -> None:
    """A seriesMaster fetched on page 1 must not be re-fetched or re-emitted
    when the same seriesMasterId appears on page 2."""
    import json as _json

    occ = {
        "subject": "Katie / Lili",
        "type": "occurrence",
        "seriesMasterId": "AAMk-master-wed",
        "start": {"dateTime": "2026-05-13T13:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T14:00:00.0000000", "timeZone": "UTC"},
    }
    master_body = {
        "id": "AAMk-master-wed",
        "iCalUId": "uid-weekly-wed@outlook.com",
        "subject": "Katie / Lili",
        "type": "seriesMaster",
        "start": {"dateTime": "2024-01-03T13:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2024-01-03T14:00:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
            "range": {"type": "noEnd", "startDate": "2024-01-03"},
        },
    }
    page1 = {
        "value": [{**occ, "id": "AAMk-occ-p1"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
    }
    page2 = {
        "value": [
            {
                **occ,
                "id": "AAMk-occ-p2",
                "start": {"dateTime": "2026-05-20T13:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-20T14:00:00.0000000", "timeZone": "UTC"},
            }
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
    }

    batch_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/$batch" in url:
            body = _json.loads(req.content)
            batch_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "responses": [
                        {"id": r["id"], "status": 200, "body": master_body}
                        for r in body["requests"]
                    ]
                },
            )
        if "page2" in url:
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    all_changes: list = []
    async for batch, _cursor in backend.initial_sync("cal-1"):
        all_changes.extend(batch)

    # Master fetched once from page 1 only; page 2 reuses cache.
    assert len(batch_calls) == 1
    # Only one seriesMaster EventChange emitted (from page 1); occurrences dropped.
    masters_emitted = [c for c in all_changes if c.event.rrule is not None]
    assert len(masters_emitted) == 1


@pytest.mark.asyncio
async def test_drain_delta_skips_synthesis_when_master_in_page() -> None:
    """When the seriesMaster is already in the delta page no $batch call is made."""
    import json as _json

    delta_body = {
        "value": [
            {
                "id": "AAMk-master-w",
                "iCalUId": "uid-w@outlook.com",
                "subject": "Weekly standup",
                "type": "seriesMaster",
                "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
                "recurrence": {
                    "pattern": {
                        "type": "weekly",
                        "interval": 1,
                        "daysOfWeek": ["wednesday"],
                    },
                    "range": {"type": "noEnd", "startDate": "2026-05-13"},
                },
            },
            {
                "id": "AAMk-occ-w",
                "iCalUId": "uid-w@outlook.com",
                "subject": "Weekly standup",
                "type": "occurrence",
                "seriesMasterId": "AAMk-master-w",
                "start": {"dateTime": "2026-05-20T09:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-20T09:30:00.0000000", "timeZone": "UTC"},
            },
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
    }

    batch_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            body = _json.loads(req.content)
            batch_calls.append(body)
            return httpx.Response(200, json={"responses": []})
        return httpx.Response(200, json=delta_body)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    collected = []
    async for batch, _cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)

    # No $batch call — master was already in the page.
    assert len(batch_calls) == 0
    # One EventChange: the seriesMaster. Occurrence is dropped.
    assert len(collected) == 1
    assert collected[0].uid == "AAMk-master-w"
    assert collected[0].event.rrule is not None


@pytest.mark.asyncio
async def test_drain_delta_exception_hydration_still_works() -> None:
    """Regression guard: exceptions still inherit subject from master, AND the
    master is now also emitted as its own EventChange (new behavior). The
    occurrence in the same page is still dropped."""
    import json as _json

    delta_body = {
        "value": [
            {
                "id": "AAMk-occ-1",
                "subject": None,
                "type": "occurrence",
                "seriesMasterId": "AAMk-master-1",
                "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-13T09:30:00.0000000", "timeZone": "UTC"},
            },
            {
                "id": "AAMk-exc-1",
                "subject": "",
                "body": {"contentType": "text", "content": ""},
                "location": {"displayName": ""},
                "type": "exception",
                "seriesMasterId": "AAMk-master-1",
                "originalStart": {
                    "dateTime": "2026-05-20T09:00:00.0000000",
                    "timeZone": "UTC",
                },
                "start": {"dateTime": "2026-05-20T10:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-05-20T10:30:00.0000000", "timeZone": "UTC"},
            },
        ],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/dl",
    }
    master_body = {
        "id": "AAMk-master-1",
        "subject": "Weekly standup",
        "body": {"contentType": "text", "content": "standup notes"},
        "location": {"displayName": "Zoom"},
        "type": "seriesMaster",
        "start": {"dateTime": "2024-01-03T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2024-01-03T09:30:00.0000000", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["wednesday"]},
            "range": {"type": "noEnd", "startDate": "2024-01-03"},
        },
    }

    batch_calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "/$batch" in str(req.url):
            body = _json.loads(req.content)
            batch_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "responses": [
                        {"id": r["id"], "status": 200, "body": master_body}
                        for r in body["requests"]
                    ]
                },
            )
        return httpx.Response(200, json=delta_body)

    backend = GraphBackend(account_id="acc-1", token_cache_json=None)
    _attach_mock(backend, handler)

    collected = []
    async for batch, _cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)

    # One $batch call for the shared master (occurrence + exception both reference it,
    # but only one fetch is needed).
    assert len(batch_calls) == 1

    # Three rows in page, two processed: synthesized master + exception override.
    # Occurrence is dropped.
    assert len(collected) == 2

    # Separate by recurrence_id presence — both have uid == "AAMk-master-1".
    overrides = [c for c in collected if c.event.recurrence_id is not None]
    masters = [c for c in collected if c.event.recurrence_id is None]
    assert len(overrides) == 1
    assert len(masters) == 1

    # Synthesized seriesMaster carries rrule.
    assert masters[0].event.rrule is not None
    assert "BYDAY=WE" in masters[0].event.rrule

    # Exception override inherited subject/description/location from master.
    assert overrides[0].event.summary == "Weekly standup"
    assert overrides[0].event.description == "standup notes"
    assert overrides[0].event.location == "Zoom"
