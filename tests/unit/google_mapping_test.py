from datetime import date, datetime, timezone

import httpx
import pytest

from lilical.backends.google import GoogleBackend, GoogleCursor, _google_event_to_change


def test_timed_event() -> None:
    data = {
        "id": "evt123",
        "iCalUID": "uid-abc@google.com",
        "summary": "Lunch",
        "status": "confirmed",
        "etag": '"abc123"',
        "sequence": 1,
        "start": {
            "dateTime": "2026-05-13T12:00:00-04:00",
            "timeZone": "America/New_York",
        },
        "end": {
            "dateTime": "2026-05-13T13:00:00-04:00",
            "timeZone": "America/New_York",
        },
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.kind == "upsert"
    assert change.uid == "uid-abc@google.com"
    assert change.event is not None
    assert change.event.summary == "Lunch"
    assert change.event.provider_event_id == "evt123"
    assert change.event.tz == "America/New_York"
    assert change.event.dtstart is not None and change.event.dtstart.tzinfo is not None
    assert change.event.dtstart.astimezone(timezone.utc) == datetime(
        2026, 5, 13, 16, 0, tzinfo=timezone.utc
    )
    assert change.event.all_day is False


def test_cancelled_event() -> None:
    data = {
        "id": "evt456",
        "iCalUID": "uid-xyz@google.com",
        "status": "cancelled",
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.kind == "delete"
    assert change.uid == "uid-xyz@google.com"


def test_no_icaluid_falls_back_to_id() -> None:
    data = {
        "id": "evt789",
        "status": "confirmed",
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.uid == "evt789"


def test_all_day_event_uses_date_not_datetime() -> None:
    data = {
        "id": "evt-day",
        "iCalUID": "uid-day@google.com",
        "summary": "Holiday",
        "status": "confirmed",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-05"},
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    e = change.event
    assert e is not None
    assert e.all_day is True
    # All-day events are stored as midnight in the local zone so .date() returns
    # the right calendar day regardless of the runner's UTC offset.
    assert e.dtstart is not None
    assert e.dtstart.tzinfo is not None
    assert e.dtstart.date() == date(2026, 7, 4)
    assert e.dtend is not None
    assert e.dtend.date() == date(2026, 7, 5)


def test_recurring_master_extracts_rrule() -> None:
    """Google returns the recurring master with a `recurrence` array; we
    extract the RRULE value (without the `RRULE:` prefix) so RecurrenceExpander
    can feed it to icalendar/recurring_ical_events."""
    data = {
        "id": "evt-rec",
        "iCalUID": "uid-rec@google.com",
        "summary": "Standup",
        "status": "confirmed",
        "start": {"dateTime": "2026-05-13T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00Z", "timeZone": "UTC"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=10", "EXDATE;TZID=UTC:20260527T090000"],
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    e = change.event
    assert e is not None
    assert e.rrule == "FREQ=WEEKLY;COUNT=10"
    assert len(e.exdates) == 1
    assert e.exdates[0].astimezone(timezone.utc) == datetime(
        2026, 5, 27, 9, 0, tzinfo=timezone.utc
    )


def test_recurring_override_is_stored_with_recurrence_id() -> None:
    """A modified instance of a recurring series carries `recurringEventId`.
    It is now stored as an override Event with recurrence_id set (from
    originalStartTime) and rrule=None, keyed under the master's iCalUID."""
    data = {
        "id": "evt-override",
        "iCalUID": "uid-rec@google.com",
        "recurringEventId": "evt-rec",
        "summary": "Standup (moved)",
        "status": "confirmed",
        "originalStartTime": {"dateTime": "2026-05-20T09:00:00Z", "timeZone": "UTC"},
        "start": {"dateTime": "2026-05-20T10:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-20T10:30:00Z", "timeZone": "UTC"},
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.recurrence_id is not None
    assert change.event.rrule is None
    assert change.event.summary == "Standup (moved)"


def test_transparent_event_maps_to_transparency() -> None:
    data = {
        "id": "evt-free",
        "iCalUID": "uid-free@google.com",
        "summary": "Free block",
        "status": "confirmed",
        "transparency": "transparent",
        "start": {"dateTime": "2026-05-13T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00Z", "timeZone": "UTC"},
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.transparency == "TRANSPARENT"


def test_tentative_status() -> None:
    data = {
        "id": "evt-tent",
        "iCalUID": "uid-tent@google.com",
        "summary": "Maybe",
        "status": "tentative",
        "start": {"dateTime": "2026-05-13T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00Z", "timeZone": "UTC"},
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert change.event.status == "TENTATIVE"


def test_attendees_extracted() -> None:
    data = {
        "id": "evt-att",
        "iCalUID": "uid-att@google.com",
        "summary": "Meet",
        "status": "confirmed",
        "start": {"dateTime": "2026-05-13T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T10:00:00Z", "timeZone": "UTC"},
        "attendees": [
            {"email": "alice@example.com", "responseStatus": "accepted"},
            {"email": "bob@example.com", "responseStatus": "needsAction"},
        ],
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None
    assert change.event is not None
    assert {a.email for a in change.event.attendees} == {
        "alice@example.com",
        "bob@example.com",
    }


# -- end-to-end: parser → EventStore → event_instances expansion --------------


def test_parsed_google_rrule_event_expands_into_instances(tmp_path) -> None:
    """Pipeline regression: prove a Google RRULE master flows through
    EventStore.apply_remote_changes to multiple EventInstanceRow rows."""
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
                kind="google",
                display_name="G",
                identity="u@example.com",
                secret_ref="acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="primary",
                display_name="Cal",
                color="#000000",
                access_role="owner",
            )
        )

    data = {
        "id": "evt-rec",
        "iCalUID": "uid-rec@google.com",
        "summary": "Weekly",
        "status": "confirmed",
        "start": {"dateTime": "2026-05-13T09:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-13T09:30:00Z", "timeZone": "UTC"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=10", "EXDATE;TZID=UTC:20260527T090000"],
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is not None

    store = EventStore(engine)
    store.apply_remote_changes("cal-1", [change], '{"sync_token": "X"}')

    with Session(engine) as session:
        instances = session.query(EventInstanceRow).all()
    # 10 occurrences minus 1 EXDATE = 9 instances inside the ±1y window.
    assert len(instances) == 9
    starts_iso = {i.dtstart_local for i in instances}
    assert "2026-05-27T09:00:00+00:00" not in starts_iso
    assert "2026-05-13T09:00:00+00:00" in starts_iso


# -- incremental_sync pagination -----------------------------------------------


def _ev(uid: str, evt_id: str) -> dict:
    return {
        "id": evt_id,
        "iCalUID": uid,
        "status": "confirmed",
        "summary": "Event",
        "etag": '"etag1"',
        "start": {"dateTime": "2026-05-14T10:00:00Z"},
        "end": {"dateTime": "2026-05-14T11:00:00Z"},
    }


def _attach_mock(backend, handler):
    backend._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend._acquire_token = lambda: "fake-token"  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_incremental_sync_reads_all_pages() -> None:
    """incremental_sync must follow nextPageToken until nextSyncToken appears."""
    page1 = {"items": [_ev("uid-1@g.com", "evt-1")], "nextPageToken": "pt-abc"}
    page2 = {"items": [_ev("uid-2@g.com", "evt-2")], "nextSyncToken": "new-sync"}
    responses = [page1, page2]
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return httpx.Response(200, json=resp)

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    changes, cursor = await backend.incremental_sync(
        "cal-1", GoogleCursor(sync_token="old")
    )

    assert len(changes) == 2
    assert {c.uid for c in changes} == {"uid-1@g.com", "uid-2@g.com"}
    assert cursor.to_json()["sync_token"] == "new-sync"


@pytest.mark.asyncio
async def test_incremental_sync_single_page_advances_token() -> None:
    """Single-page response still advances the sync token."""
    page1 = {"items": [_ev("uid-1@g.com", "evt-1")], "nextSyncToken": "tok-next"}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page1)

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    changes, cursor = await backend.incremental_sync(
        "cal-1", GoogleCursor(sync_token="old")
    )

    assert len(changes) == 1
    assert cursor.to_json()["sync_token"] == "tok-next"


# ── write path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_event_inserts_with_send_updates_none() -> None:
    from lilical.models.event import Event

    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "id": "prov-id-1",
                "iCalUID": "uid-1@google.com",
                "summary": "Meeting",
                "etag": '"etag1"',
            },
        )

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    event = Event(uid="uid-1@google.com", calendar_id="cal-1", summary="Meeting")
    result = await backend.create_event("cal-1", event)

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert "sendUpdates=none" in str(req.url)
    assert "/calendars/" in req.url.path
    assert result.uid == "uid-1@google.com"
    assert result.provider_event_id == "prov-id-1"


@pytest.mark.asyncio
async def test_update_event_calls_service_update() -> None:
    from lilical.models.event import Event

    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            json={
                "id": "prov-id-2",
                "iCalUID": "uid-2@google.com",
                "summary": "Updated",
                "etag": '"etag2"',
            },
        )

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    event = Event(
        uid="uid-2@google.com",
        calendar_id="cal-1",
        provider_event_id="prov-id-2",
        summary="Updated",
    )
    await backend.update_event("cal-1", event, if_match='"etag1"')

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "PUT"
    assert "/events/prov-id-2" in req.url.path
    assert "sendUpdates=none" in str(req.url)
    assert req.headers.get("if-match") == '"etag1"'


@pytest.mark.asyncio
async def test_update_event_412_maps_to_conflict_error() -> None:
    from lilical.backends.base import ConflictError
    from lilical.models.event import Event

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(412, json={"error": {"message": "Precondition Failed"}})

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    event = Event(uid="u1", calendar_id="cal-1", provider_event_id="p1")
    with pytest.raises(ConflictError):
        await backend.update_event("cal-1", event, if_match='"old"')


@pytest.mark.asyncio
async def test_delete_event_calls_service_delete() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    await backend.delete_event("cal-1", "uid-to-delete", if_match='"etag"')

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "DELETE"
    assert "/events/uid-to-delete" in req.url.path
    assert "sendUpdates=none" in str(req.url)


@pytest.mark.asyncio
async def test_429_user_rate_limit_maps_to_transient() -> None:
    from lilical.backends.base import TransientError

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Rate Limited"}})

    backend = GoogleBackend("acc-1", token_json=None)
    _attach_mock(backend, handler)

    with pytest.raises(TransientError):
        await backend.delete_event("cal-1", "u1", if_match=None)
