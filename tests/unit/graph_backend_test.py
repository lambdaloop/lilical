from __future__ import annotations

import json

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
    assert change.uid == "uid-1@outlook.com"
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
    assert change.uid == "uid-2@outlook.com"


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

    assert [c.uid for c in collected] == ["u1", "u2", "u3"]
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
    assert cals == [
        {"id": "AAA", "display_name": "Work", "provider_id": "AAA"},
        {"id": "BBB", "display_name": "Personal", "provider_id": "BBB"},
    ]
