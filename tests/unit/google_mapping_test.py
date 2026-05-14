from datetime import datetime, timezone

from lilical.backends.google import _google_event_to_change


def test_timed_event() -> None:
    data = {
        "id": "evt123",
        "iCalUID": "uid-abc@google.com",
        "summary": "Lunch",
        "status": "confirmed",
        "etag": '"abc123"',
        "sequence": 1,
        "start": {"dateTime": "2026-05-13T12:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-05-13T13:00:00-04:00", "timeZone": "America/New_York"},
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
    # naive datetime at midnight — _ensure_aware_dt treats as UTC midnight.
    assert e.dtstart == datetime(2026, 7, 4, 0, 0)
    assert e.dtend == datetime(2026, 7, 5, 0, 0)


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
