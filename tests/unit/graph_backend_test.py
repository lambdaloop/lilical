from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    PermanentError,
    TransientError,
)
from lilical.backends.graph import (
    GraphBackend,
    GraphCursor,
    _graph_event_to_change,
)


def _attach_mock(backend: GraphBackend, handler) -> None:
    """Wire a MockTransport into the backend's httpx client and stub out auth."""
    backend._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend._acquire_token = lambda: "fake-token"  # type: ignore[method-assign]


# -- mapping ------------------------------------------------------------------


def test_event_to_change_upsert() -> None:
    data = {
        "id": "AAMk-abc",
        "iCalUId": "uid-1@outlook.com",
        "subject": "Standup",
        "body": {"content": "weekly", "contentType": "text"},
        "location": {"displayName": "Zoom"},
        "@odata.etag": 'W/"abc"',
    }
    change = _graph_event_to_change(data, "cal-1")
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
    data = {
        "id": "AAMk-xyz",
        "iCalUId": "uid-2@outlook.com",
        "@removed": {"reason": "deleted"},
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change.kind == "delete"
    assert change.uid == "AAMk-xyz"


def test_event_to_change_falls_back_to_id_for_uid() -> None:
    data = {"id": "AAMk-foo", "subject": "x"}
    change = _graph_event_to_change(data, "cal-1")
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
    _attach_mock(backend, handler)

    collected = []
    last_cursor: GraphCursor | None = None
    async for batch, cursor in backend.initial_sync("cal-1"):
        collected.extend(batch)
        last_cursor = cursor

    # Graph local uid comes from `id`, not `iCalUId`.
    assert [c.uid for c in collected] == ["e1", "e2", "e3"]
    assert last_cursor is not None
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
    assert j == {"delta_link": "https://example/delta?token=X"}
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
    data = {
        "id": "AAMk-timed",
        "iCalUId": "uid-timed@outlook.com",
        "subject": "Review",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "America/New_York"},
        "isAllDay": False,
        "showAs": "busy",
        "isCancelled": False,
        "webLink": "https://outlook.office365.com/...",
        "lastModifiedDateTime": "2026-05-12T08:00:00.1234567Z",
    }
    change = _graph_event_to_change(data, "cal-1")
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
    data = {
        "id": "AAMk-allday",
        "iCalUId": "uid-allday@outlook.com",
        "subject": "Holiday",
        "start": {"dateTime": "2026-07-04T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-07-05T00:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": True,
    }
    change = _graph_event_to_change(data, "cal-1")
    e = change.event
    assert e is not None
    assert e.all_day is True
    assert e.dtstart == datetime(2026, 7, 4, tzinfo=timezone.utc)
    assert e.dtend == datetime(2026, 7, 5, tzinfo=timezone.utc)


def test_event_to_change_marks_cancelled() -> None:
    data = {
        "id": "AAMk-cancel",
        "subject": "Killed meeting",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "isCancelled": True,
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change.event is not None
    assert change.event.status == "CANCELLED"


def test_event_to_change_show_as_free_is_transparent() -> None:
    data = {
        "id": "AAMk-free",
        "start": {"dateTime": "2026-05-13T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00.0000000", "timeZone": "UTC"},
        "showAs": "free",
    }
    change = _graph_event_to_change(data, "cal-1")
    assert change.event is not None
    assert change.event.transparency == "TRANSPARENT"


def test_event_to_change_extracts_categories_and_attendees() -> None:
    data = {
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
    assert change.event is not None
    assert set(change.event.categories) == {"Work", "Important"}
    assert set(change.event.attendees) == {"alice@example.com", "bob@example.com"}


def test_event_to_change_recurring_occurrences_have_distinct_uids() -> None:
    """All occurrences of a recurring series share iCalUId but have distinct
    `id` values. Local uid must come from `id` so apply_remote_changes doesn't
    collapse them into one row."""
    occurrences = [
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
    uids = [_graph_event_to_change(o, "cal-1").uid for o in occurrences]
    assert uids == ["AAMk-occ-0", "AAMk-occ-1", "AAMk-occ-2"]
    assert len(set(uids)) == 3


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

    data = {
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
