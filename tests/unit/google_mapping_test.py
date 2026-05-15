from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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


def test_all_day_event_uses_date_not_dateTime() -> None:
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


def test_recurring_override_is_skipped() -> None:
    """A modified instance of a recurring series carries `recurringEventId`
    pointing at the master. Storage doesn't yet key on recurrence_id, so we
    drop overrides at the change layer — same as CalDAV's RECURRENCE-ID skip
    — to keep them from clobbering the master."""
    data = {
        "id": "evt-override",
        "iCalUID": "uid-rec@google.com",
        "recurringEventId": "evt-rec",
        "summary": "Standup (moved)",
        "status": "confirmed",
        "start": {"dateTime": "2026-05-20T10:00:00Z", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-20T10:30:00Z", "timeZone": "UTC"},
    }
    change = _google_event_to_change(data, "cal-1")
    assert change is None


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
    assert set(change.event.attendees) == {"alice@example.com", "bob@example.com"}


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


@pytest.mark.asyncio
async def test_incremental_sync_reads_all_pages() -> None:
    """incremental_sync must follow nextPageToken until nextSyncToken appears."""
    req1, req2 = MagicMock(), MagicMock()
    page1 = {"items": [_ev("uid-1@g.com", "evt-1")], "nextPageToken": "pt-abc"}
    page2 = {"items": [_ev("uid-2@g.com", "evt-2")], "nextSyncToken": "new-sync"}

    events_res = MagicMock()
    events_res.list.return_value = req1
    events_res.list_next.side_effect = lambda req, resp: (
        req2 if "nextPageToken" in resp else None
    )

    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    execute_map = {req1: page1, req2: page2}
    backend._execute = AsyncMock(side_effect=lambda req: execute_map[req])

    changes, cursor = await backend.incremental_sync(
        "cal-1", GoogleCursor(sync_token="old")
    )

    assert len(changes) == 2
    assert {c.uid for c in changes} == {"uid-1@g.com", "uid-2@g.com"}
    assert cursor.to_json()["sync_token"] == "new-sync"


@pytest.mark.asyncio
async def test_incremental_sync_single_page_advances_token() -> None:
    """Single-page response still advances the sync token."""
    req1 = MagicMock()
    page1 = {"items": [_ev("uid-1@g.com", "evt-1")], "nextSyncToken": "tok-next"}

    events_res = MagicMock()
    events_res.list.return_value = req1
    events_res.list_next.return_value = None

    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(return_value=page1)

    changes, cursor = await backend.incremental_sync(
        "cal-1", GoogleCursor(sync_token="old")
    )

    assert len(changes) == 1
    assert cursor.to_json()["sync_token"] == "tok-next"


# ── write path ────────────────────────────────────────────────────────────────


def _make_http_error(status: int) -> "HttpError":
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


@pytest.mark.asyncio
async def test_create_event_inserts_with_send_updates_none() -> None:
    from lilical.models.event import Event

    insert_calls: list[dict] = []

    def _fake_insert(**kwargs):
        insert_calls.append(kwargs)
        req = MagicMock()
        req.execute.return_value = {
            "id": "prov-id-1",
            "iCalUID": "uid-1@google.com",
            "summary": "Meeting",
            "etag": '"etag1"',
        }
        return req

    events_res = MagicMock()
    events_res.insert.side_effect = _fake_insert
    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(side_effect=lambda req: req.execute())

    event = Event(uid="uid-1@google.com", calendar_id="cal-1", summary="Meeting")
    result = await backend.create_event("cal-1", event)

    assert len(insert_calls) == 1
    assert insert_calls[0]["sendUpdates"] == "none"
    assert insert_calls[0]["calendarId"] == "cal-1"
    assert result.uid == "uid-1@google.com"
    assert result.provider_event_id == "prov-id-1"


@pytest.mark.asyncio
async def test_update_event_calls_service_update() -> None:
    from lilical.models.event import Event

    update_calls: list[dict] = []

    def _fake_update(**kwargs):
        update_calls.append(kwargs)
        req = MagicMock()
        req.execute.return_value = {
            "id": "prov-id-2",
            "iCalUID": "uid-2@google.com",
            "summary": "Updated",
            "etag": '"etag2"',
        }
        return req

    events_res = MagicMock()
    events_res.update.side_effect = _fake_update
    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(side_effect=lambda req: req.execute())

    event = Event(
        uid="uid-2@google.com",
        calendar_id="cal-1",
        provider_event_id="prov-id-2",
        summary="Updated",
    )
    await backend.update_event("cal-1", event, if_match='"etag1"')

    assert len(update_calls) == 1
    assert update_calls[0]["calendarId"] == "cal-1"
    assert update_calls[0]["eventId"] == "prov-id-2"
    assert update_calls[0]["sendUpdates"] == "none"


@pytest.mark.asyncio
async def test_update_event_412_maps_to_conflict_error() -> None:
    from lilical.backends.base import ConflictError
    from lilical.models.event import Event

    events_res = MagicMock()
    req = MagicMock()
    req.execute.side_effect = _make_http_error(412)
    events_res.update.return_value = req
    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(side_effect=lambda r: r.execute())

    event = Event(uid="u1", calendar_id="cal-1", provider_event_id="p1")
    with pytest.raises(ConflictError):
        await backend.update_event("cal-1", event, if_match='"old"')


@pytest.mark.asyncio
async def test_delete_event_calls_service_delete() -> None:
    delete_calls: list[dict] = []

    def _fake_delete(**kwargs):
        delete_calls.append(kwargs)
        req = MagicMock()
        req.execute.return_value = None
        return req

    events_res = MagicMock()
    events_res.delete.side_effect = _fake_delete
    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(side_effect=lambda req: req.execute())

    await backend.delete_event("cal-1", "uid-to-delete", if_match='"etag"')

    assert len(delete_calls) == 1
    assert delete_calls[0]["calendarId"] == "cal-1"
    assert delete_calls[0]["eventId"] == "uid-to-delete"


@pytest.mark.asyncio
async def test_429_user_rate_limit_maps_to_transient() -> None:
    from lilical.backends.base import TransientError

    events_res = MagicMock()
    req = MagicMock()
    req.execute.side_effect = _make_http_error(429)
    events_res.delete.return_value = req
    service = MagicMock()
    service.events.return_value = events_res

    backend = GoogleBackend("acc-1", token_json=None)
    backend._ensure_service = AsyncMock(return_value=service)
    backend._execute = AsyncMock(side_effect=lambda r: r.execute())

    with pytest.raises(TransientError):
        await backend.delete_event("cal-1", "u1", if_match=None)
