"""Tests for Google Calendar: serializer and GoogleBackend write methods."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx

from lilical.backends._google_serializer import event_to_google_body
from lilical.models.event import Event

# ── Serializer pure-function tests ────────────────────────────────────────────


def test_serializer_full_body():
    event = Event(
        uid="uid-test",
        calendar_id="cal-1",
        summary="Meeting",
        description="Discuss stuff",
        location="Office",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
        tz="America/New_York",
        status="CONFIRMED",
        transparency="TRANSPARENT",
        color="#e05050",
        url="https://example.com",
    )
    body = event_to_google_body(event)

    assert body["summary"] == "Meeting"
    assert body["description"] == "Discuss stuff"
    assert body["start"]["dateTime"] is not None
    assert body["start"]["timeZone"] == "America/New_York"
    assert body["transparency"] == "transparent"
    assert body["colorId"] == "11"
    assert body["status"] == "confirmed"
    assert body["source"]["url"] == "https://example.com"


def test_serializer_all_day():
    event = Event(
        uid="uid-allday",
        calendar_id="cal-1",
        summary="Birthday",
        dtstart=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc),
        tz="UTC",
        all_day=True,
    )
    body = event_to_google_body(event)

    assert body["start"] == {"date": "2026-05-14"}
    assert "dateTime" not in body["start"]
    assert body["end"] == {"date": "2026-05-15"}


def test_serializer_recurrence():
    exdate_dt = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    event = Event(
        uid="uid-rec",
        calendar_id="cal-1",
        summary="Weekly standup",
        dtstart=datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 4, 9, 30, tzinfo=timezone.utc),
        rrule="FREQ=WEEKLY;BYDAY=MO,WE",
        exdates=(exdate_dt,),
    )
    body = event_to_google_body(event)

    assert "recurrence" in body
    lines = body["recurrence"]
    assert isinstance(lines, list)
    assert lines[0].startswith("RRULE:")
    assert any(line.startswith("EXDATE:") for line in lines)


def test_serializer_no_color_no_color_id():
    event = Event(
        uid="uid-nocolor",
        calendar_id="cal-1",
        summary="No color event",
        color=None,
    )
    body = event_to_google_body(event)
    assert "colorId" not in body


def test_serializer_rdate():
    """RDATE entries produce RDATE: lines in recurrence array."""
    rdate = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    event = Event(
        uid="uid-rdate",
        calendar_id="cal-1",
        summary="With RDATE",
        dtstart=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        rrule="FREQ=WEEKLY;COUNT=2",
        rdates=(rdate,),
    )
    body = event_to_google_body(event)
    lines = body["recurrence"]
    assert any(line.startswith("RDATE:") for line in lines)


def test_serializer_recurring_event_id():
    """Override events get recurringEventId set."""
    event = Event(
        uid="uid-master",
        calendar_id="cal-1",
        summary="Moved occurrence",
        recurrence_id=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        dtstart=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
    )
    body = event_to_google_body(event)
    assert body["recurringEventId"] == "uid-master"


def test_serializer_dt_to_google_none():
    """_dt_to_google(None) returns an empty dict."""
    from lilical.backends._google_serializer import _dt_to_google

    assert _dt_to_google(None, "UTC", False) == {}


def test_serializer_exdate_non_utc_tz():
    """EXDATE with non-UTC timezone includes TZID parameter."""
    from lilical.backends._google_serializer import _format_exdate

    dt = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
    result = _format_exdate(dt, "America/New_York")
    assert "TZID=America/New_York" in result
    # UTC path still works
    result_utc = _format_exdate(dt, "UTC")
    assert "TZID=UTC" not in result_utc
    assert result_utc.endswith("Z")


def test_serializer_exdate_none_tz():
    """EXDATE with tz_name=None falls back to UTC format."""
    from lilical.backends._google_serializer import _format_exdate

    dt = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
    result = _format_exdate(dt, None)
    assert "TZID" not in result
    assert result.endswith("Z")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _attach_mock(backend, handler):
    backend._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend._acquire_token = lambda: "fake-token"  # type: ignore[method-assign]


def _make_backend():
    from lilical.backends.google import GoogleBackend

    return GoogleBackend(account_id="test-account")


# ── GoogleBackend write tests ─────────────────────────────────────────────────


def test_create_event_sends_full_body():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "iCalUID": "uid-x",
                "id": "server-id",
                "etag": '"etag"',
                "sequence": 0,
            },
        )

    backend = _make_backend()
    _attach_mock(backend, handler)

    event = Event(
        uid="local-uid",
        calendar_id="cal-1",
        summary="New Event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )

    async def _run():
        try:
            return await backend.create_event("cal-1", event)
        finally:
            await backend.aclose()

    result = asyncio.run(_run())

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert "/calendars/" in req.url.path
    assert "/events" in req.url.path
    assert "sendUpdates=none" in str(req.url)
    body = json.loads(req.content)
    assert "summary" in body
    assert "start" in body
    assert "end" in body
    assert result.provider_event_id == "server-id"


def test_update_event_uses_provider_event_id():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "iCalUID": "uid-y",
                "id": "pid-123",
                "etag": '"etag2"',
                "sequence": 1,
            },
        )

    backend = _make_backend()
    _attach_mock(backend, handler)

    event = Event(
        uid="local-uid-y",
        calendar_id="cal-1",
        provider_event_id="pid-123",
        summary="Existing Event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )

    async def _run():
        try:
            await backend.update_event("cal-1", event, None)
        finally:
            await backend.aclose()

    asyncio.run(_run())

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "PUT"
    assert "/events/pid-123" in req.url.path
    assert "sendUpdates=none" in str(req.url)


def test_delete_event_uses_provider_event_id():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    backend = _make_backend()
    _attach_mock(backend, handler)

    async def _run():
        try:
            await backend.delete_event("cal-1", "pid-456", None)
        finally:
            await backend.aclose()

    asyncio.run(_run())

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "DELETE"
    assert "/events/pid-456" in req.url.path
    assert "sendUpdates=none" in str(req.url)


# ── Per-occurrence (instance) ops ─────────────────────────────────────────────


from zoneinfo import ZoneInfo  # noqa: E402


def _instance_handler(captured, *, instance_etag='"inst-etag"'):
    """Mock that answers the instances GET then the PATCH/DELETE."""

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.method == "GET" and "/instances" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "master_20260311", "etag": instance_etag}]
                },
            )
        # PATCH returns the instance body; DELETE returns 204
        if req.method == "PATCH":
            return httpx.Response(
                200,
                json={"iCalUID": "u", "id": "master_20260311", "etag": '"e"'},
            )
        return httpx.Response(204)

    return handler


def test_update_instance_allday_uses_date_originalstart():
    captured: list[httpx.Request] = []
    backend = _make_backend()
    _attach_mock(backend, _instance_handler(captured))

    rid = datetime(2026, 3, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    event = Event(
        uid="m",
        calendar_id="cal-1",
        provider_event_id="master",
        summary="Birthday",
        dtstart=rid,
        dtend=rid,
        all_day=True,
        recurrence_id=rid,
    )

    async def _run():
        try:
            await backend.update_instance("cal-1", "master", rid, event)
        finally:
            await backend.aclose()

    asyncio.run(_run())

    get_req = next(r for r in captured if "/instances" in r.url.path)
    # All-day: originalStart must be a bare date, never a Z dateTime.
    assert get_req.url.params["originalStart"] == "2026-03-11"
    patch_req = next(r for r in captured if r.method == "PATCH")
    assert patch_req.headers.get("If-Match") == '"inst-etag"'


def test_update_instance_timed_uses_utc_datetime_originalstart():
    captured: list[httpx.Request] = []
    backend = _make_backend()
    _attach_mock(backend, _instance_handler(captured))

    rid = datetime(2026, 3, 11, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    event = Event(
        uid="m",
        calendar_id="cal-1",
        provider_event_id="master",
        summary="Standup",
        dtstart=rid,
        dtend=rid,
        recurrence_id=rid,
    )

    async def _run():
        try:
            await backend.update_instance("cal-1", "master", rid, event)
        finally:
            await backend.aclose()

    asyncio.run(_run())

    get_req = next(r for r in captured if "/instances" in r.url.path)
    # Timed: NY 09:00 EDT == 13:00 UTC.
    assert get_req.url.params["originalStart"] == "2026-03-11T13:00:00Z"


def test_delete_instance_allday_uses_date_originalstart():
    captured: list[httpx.Request] = []
    backend = _make_backend()
    _attach_mock(backend, _instance_handler(captured))

    rid = datetime(2026, 3, 11, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    async def _run():
        try:
            await backend.delete_instance("cal-1", "master", rid, all_day=True)
        finally:
            await backend.aclose()

    asyncio.run(_run())

    get_req = next(r for r in captured if "/instances" in r.url.path)
    assert get_req.url.params["originalStart"] == "2026-03-11"
    del_req = next(r for r in captured if r.method == "DELETE")
    assert del_req.headers.get("If-Match") == '"inst-etag"'


def test_delete_event_sends_if_match_when_provided():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    backend = _make_backend()
    _attach_mock(backend, handler)

    async def _run():
        try:
            await backend.delete_event("cal-1", "pid-789", '"etag-9"')
        finally:
            await backend.aclose()

    asyncio.run(_run())

    assert captured[0].headers.get("If-Match") == '"etag-9"'
