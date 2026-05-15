"""Tests for Google Calendar: serializer and GoogleBackend write methods."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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


# ── Fake service and GoogleBackend write tests ────────────────────────────────


class _FakeService:
    """Records calls to service.events().insert/update/delete/patch/instances."""

    def __init__(self, resp=None):
        self._resp = resp or {}
        self.calls = []

    def events(self):
        return self

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        return self

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return self

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return self

    def patch(self, **kwargs):
        self.calls.append(("patch", kwargs))
        return self

    def instances(self, **kwargs):
        self.calls.append(("instances", kwargs))
        return self

    def execute(self):
        return self._resp


def _make_backend():
    from lilical.backends.google import GoogleBackend

    return GoogleBackend(account_id="test-account")


def test_create_event_sends_full_body():
    fake = _FakeService(
        resp={
            "iCalUID": "uid-x",
            "id": "server-id",
            "etag": '"etag"',
            "sequence": 0,
        }
    )

    backend = _make_backend()

    async def _fake_ensure():
        return fake

    async def _fake_execute(req):
        return req.execute()

    backend._ensure_service = _fake_ensure
    backend._execute = _fake_execute

    event = Event(
        uid="local-uid",
        calendar_id="cal-1",
        summary="New Event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )

    result = asyncio.run(backend.create_event("cal-1", event))

    insert_calls = [c for c in fake.calls if c[0] == "insert"]
    assert len(insert_calls) == 1
    kwargs = insert_calls[0][1]
    body = kwargs["body"]
    assert "summary" in body
    assert "start" in body
    assert "end" in body
    assert result.provider_event_id == "server-id"


def test_update_event_uses_provider_event_id():
    fake = _FakeService(
        resp={
            "iCalUID": "uid-y",
            "id": "pid-123",
            "etag": '"etag2"',
            "sequence": 1,
        }
    )

    backend = _make_backend()

    async def _fake_ensure():
        return fake

    async def _fake_execute(req):
        return req.execute()

    backend._ensure_service = _fake_ensure
    backend._execute = _fake_execute

    event = Event(
        uid="local-uid-y",
        calendar_id="cal-1",
        provider_event_id="pid-123",
        summary="Existing Event",
        dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
    )

    asyncio.run(backend.update_event("cal-1", event, None))

    update_calls = [c for c in fake.calls if c[0] == "update"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["eventId"] == "pid-123"


def test_delete_event_uses_provider_event_id():
    fake = _FakeService(resp={})

    backend = _make_backend()

    async def _fake_ensure():
        return fake

    async def _fake_execute(req):
        return req.execute()

    backend._ensure_service = _fake_ensure
    backend._execute = _fake_execute

    asyncio.run(backend.delete_event("cal-1", "pid-456", None))

    delete_calls = [c for c in fake.calls if c[0] == "delete"]
    assert len(delete_calls) == 1
    assert delete_calls[0][1]["eventId"] == "pid-456"
