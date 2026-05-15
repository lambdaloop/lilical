"""Tests for Google Calendar: serializer and GoogleBackend write methods."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

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

    result = asyncio.run(backend.create_event("cal-1", event))

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

    asyncio.run(backend.update_event("cal-1", event, None))

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

    asyncio.run(backend.delete_event("cal-1", "pid-456", None))

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "DELETE"
    assert "/events/pid-456" in req.url.path
    assert "sendUpdates=none" in str(req.url)
