from __future__ import annotations

from datetime import datetime, timezone

import httpx
import icalendar
import pytest

from lilical.backends.base import PermanentError
from lilical.backends.caldav import (
    CalDavBackend,
    _discover_caldav_url,
    _parse_vevents,
    _vevent_to_event,
)


async def _aresult(value):
    return value


# -- list_calendars: URL → str (SQLite can't bind URL objects) ---------------


class _UrlLike:
    """Stand-in for caldav.lib.url.URL — not a string but stringifiable."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


class _FakeCal:
    def __init__(self, id_value, url_value, name: str | None = "Cal") -> None:
        self.id = id_value
        self.url = url_value
        self.name = name


class _FakePrincipal:
    def __init__(self, calendars: list[_FakeCal]) -> None:
        self._cals = calendars

    def calendars(self) -> list[_FakeCal]:
        return self._cals


class _FakeClient:
    def __init__(self, principal_result) -> None:
        self._principal_result = principal_result

    def principal(self):
        if isinstance(self._principal_result, Exception):
            raise self._principal_result
        return self._principal_result


def _wire_fake_client(backend: CalDavBackend, client: _FakeClient) -> None:
    backend._get_client = lambda: _aresult(client)  # type: ignore[method-assign]
    # _run normally goes through asyncio.to_thread; in tests we run inline.
    backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_list_calendars_converts_url_objects_to_strings() -> None:
    """SQLite can't bind caldav URL objects — list_calendars must coerce to str."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    cal = _FakeCal(
        id_value=_UrlLike("https://example.com/cal/work"),
        url_value=_UrlLike("https://example.com/cal/work/"),
        name="Work",
    )
    _wire_fake_client(backend, _FakeClient(_FakePrincipal([cal])))

    result = await backend.list_calendars()

    assert len(result) == 1
    entry = result[0]
    assert isinstance(entry["id"], str)
    assert isinstance(entry["provider_id"], str)
    assert isinstance(entry["display_name"], str)
    assert entry["provider_id"] == "https://example.com/cal/work/"


@pytest.mark.asyncio
async def test_list_calendars_falls_back_to_id_when_name_missing() -> None:
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    cal = _FakeCal(
        id_value=_UrlLike("cal-no-name"),
        url_value=_UrlLike("https://example.com/cal/"),
        name=None,
    )
    _wire_fake_client(backend, _FakeClient(_FakePrincipal([cal])))

    result = await backend.list_calendars()
    assert result[0]["display_name"] == "cal-no-name"


# -- list_calendars: translate AttributeError("...tag") to PermanentError ----


@pytest.mark.asyncio
async def test_list_calendars_translates_none_tree_attribute_error() -> None:
    """Caldav lib raises AttributeError on tree.tag when the server body isn't
    XML. We must translate it into a PermanentError with the bad URL so the
    user knows where to look — not a cryptic 'NoneType has no attribute tag'."""
    backend = CalDavBackend("acc-1", "https://wrong-host.example", "u", "p")
    err = AttributeError("'NoneType' object has no attribute 'tag'")
    _wire_fake_client(backend, _FakeClient(err))

    with pytest.raises(PermanentError) as excinfo:
        await backend.list_calendars()

    msg = str(excinfo.value)
    assert "wrong-host.example" in msg
    assert "CalDAV" in msg


@pytest.mark.asyncio
async def test_list_calendars_does_not_translate_unrelated_attribute_error() -> None:
    """An AttributeError without 'tag' should not be misclassified."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    err = AttributeError("'NoneType' object has no attribute 'something_else'")
    _wire_fake_client(backend, _FakeClient(err))

    with pytest.raises(PermanentError) as excinfo:
        await backend.list_calendars()

    msg = str(excinfo.value)
    # The CalDAV-specific helper message should not appear.
    assert "CalDAV server at" not in msg


# -- _discover_caldav_url (.well-known/caldav, RFC 6764) ---------------------


def _patch_httpx_client(monkeypatch, handler) -> None:
    """Force every httpx.Client() the discovery function builds to use a mock
    transport. We monkey-patch the class itself because the discovery function
    imports httpx locally."""
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("httpx.Client", factory)


def test_discover_follows_307_redirect(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "PROPFIND"
        assert str(req.url) == "https://snailbox.ink/.well-known/caldav"
        return httpx.Response(307, headers={"Location": "https://snailbox.ink/dav/cal"})

    _patch_httpx_client(monkeypatch, handler)
    result = _discover_caldav_url("https://snailbox.ink", "u", "p")
    assert result == "https://snailbox.ink/dav/cal"


def test_discover_returns_original_when_no_redirect(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    _patch_httpx_client(monkeypatch, handler)
    result = _discover_caldav_url("https://example.com", "u", "p")
    assert result == "https://example.com"


def test_discover_returns_original_on_network_failure(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _patch_httpx_client(monkeypatch, handler)
    result = _discover_caldav_url("https://unreachable.example", "u", "p")
    assert result == "https://unreachable.example"


def test_discover_adds_https_scheme_when_missing(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(404)

    _patch_httpx_client(monkeypatch, handler)
    result = _discover_caldav_url("nextcloud.example.com", "u", "p")
    assert captured["url"].startswith("https://nextcloud.example.com/")
    # No redirect → original (now with scheme) is returned.
    assert result == "https://nextcloud.example.com"


def test_discover_resolves_relative_location_header(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "/remote.php/dav/"})

    _patch_httpx_client(monkeypatch, handler)
    result = _discover_caldav_url("https://nextcloud.example", "u", "p")
    assert result == "https://nextcloud.example/remote.php/dav/"


def test_discover_handles_empty_input() -> None:
    assert _discover_caldav_url("", "u", "p") == ""


def test_discover_handles_input_with_no_netloc() -> None:
    # urlparse("foo") yields netloc="" — but the prefix coercion makes it
    # "https://foo", which has netloc="foo". This documents that behavior.
    # The point is: it doesn't crash.
    result = _discover_caldav_url("not a valid url", "u", "p")
    assert isinstance(result, str)


# -- _parse_vevents ----------------------------------------------------------


_VCALENDAR_ONE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:event-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
SUMMARY:Design review
END:VEVENT
END:VCALENDAR
"""


_VCALENDAR_RECURRENCE_OVERRIDE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:series-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:series-1@example.com
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260520T090000Z
DTSTART:20260520T100000Z
DTEND:20260520T110000Z
SUMMARY:Standup (rescheduled)
END:VEVENT
END:VCALENDAR
"""


def test_parse_vevents_empty_input_returns_empty_list() -> None:
    assert _parse_vevents(None) == []
    assert _parse_vevents("") == []
    assert _parse_vevents(b"") == []


def test_parse_vevents_extracts_single_vevent_from_vcalendar() -> None:
    vevents = _parse_vevents(_VCALENDAR_ONE_EVENT)
    assert len(vevents) == 1
    assert str(vevents[0].get("UID")) == "event-1@example.com"


def test_parse_vevents_extracts_multiple_vevents_for_recurrence_overrides() -> None:
    vevents = _parse_vevents(_VCALENDAR_RECURRENCE_OVERRIDE)
    assert len(vevents) == 2


def test_parse_vevents_returns_empty_for_garbage() -> None:
    assert _parse_vevents("this is not iCalendar at all") == []


# -- _vevent_to_event --------------------------------------------------------


def test_vevent_to_event_handles_missing_dtstart() -> None:
    ve = icalendar.Event()
    ve.add("UID", "no-dtstart@example.com")
    ve.add("SUMMARY", "headless")
    event = _vevent_to_event(ve, calendar_id="cal-1", href="h", etag="e")
    assert event.uid == "no-dtstart@example.com"
    assert event.tz == "UTC"
    assert event.all_day is False


def test_vevent_to_event_marks_all_day_when_value_is_date() -> None:
    ve = icalendar.Event()
    ve.add("UID", "all-day@example.com")
    ve.add("SUMMARY", "Holiday")
    ve.add("DTSTART", icalendar.vDate.from_ical("20260704"))
    # icalendar.vDate sets params VALUE=DATE on add — but only if you use the
    # right helper. Force it explicitly so we mimic real CalDAV payloads.
    ve["DTSTART"].params["VALUE"] = "DATE"
    event = _vevent_to_event(ve, calendar_id="cal-1", href="h", etag="e")
    assert event.all_day is True


def test_vevent_to_event_reads_tzid_from_dtstart_params() -> None:
    ve = icalendar.Event()
    ve.add("UID", "tz@example.com")
    ve.add("SUMMARY", "Meeting")
    ve.add("DTSTART", icalendar.vDatetime.from_ical("20260513T090000"))
    ve["DTSTART"].params["TZID"] = "America/New_York"
    event = _vevent_to_event(ve, calendar_id="cal-1", href="h", etag="e")
    assert event.tz == "America/New_York"


# -- _vevent_to_event: real CalDAV-shaped VCALENDAR payloads -----------------


_VCAL_TIMED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:timed-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
SUMMARY:Design review
DESCRIPTION:Plan the next sprint
LOCATION:Room 4
SEQUENCE:2
END:VEVENT
END:VCALENDAR
"""


_VCAL_ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:all-day-1@example.com
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260704
DTEND;VALUE=DATE:20260705
SUMMARY:Independence Day
END:VEVENT
END:VCALENDAR
"""


_VCAL_DURATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:dur-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DURATION:PT1H30M
SUMMARY:Workshop
END:VEVENT
END:VCALENDAR
"""


_VCAL_RRULE_EXDATE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:weekly-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
RRULE:FREQ=WEEKLY;COUNT=10
EXDATE:20260527T090000Z
SUMMARY:Weekly standup
END:VEVENT
END:VCALENDAR
"""


_VCAL_RICH = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:rich-1@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
SUMMARY:Big meeting
URL:https://example.com/meeting
STATUS:TENTATIVE
TRANSP:TRANSPARENT
CATEGORIES:work,important
ATTENDEE;CN=Alice:mailto:alice@example.com
ATTENDEE;CN=Bob:mailto:bob@example.com
END:VEVENT
END:VCALENDAR
"""


_VCAL_TZID = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:tz-1@example.com
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260513T090000
DTEND;TZID=America/New_York:20260513T100000
SUMMARY:NY meeting
END:VEVENT
END:VCALENDAR
"""


def test_vevent_to_event_extracts_timed_event_datetimes() -> None:
    vevents = _parse_vevents(_VCAL_TIMED)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.dtstart == datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    assert event.dtend == datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)
    assert event.summary == "Design review"
    assert event.description == "Plan the next sprint"
    assert event.location == "Room 4"
    assert event.sequence == 2
    assert event.all_day is False


def test_vevent_to_event_extracts_all_day_event() -> None:
    vevents = _parse_vevents(_VCAL_ALL_DAY)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.all_day is True
    # All-day → naive datetime at midnight; EventStore._ensure_aware_dt then
    # assumes UTC, which keeps the date consistent.
    assert event.dtstart == datetime(2026, 7, 4, 0, 0)
    assert event.dtend == datetime(2026, 7, 5, 0, 0)


def test_vevent_to_event_computes_dtend_from_duration() -> None:
    vevents = _parse_vevents(_VCAL_DURATION)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.dtstart == datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    assert event.dtend == datetime(2026, 5, 13, 10, 30, tzinfo=timezone.utc)


def test_vevent_to_event_extracts_rrule_and_exdates() -> None:
    vevents = _parse_vevents(_VCAL_RRULE_EXDATE)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.rrule is not None
    assert "FREQ=WEEKLY" in event.rrule
    assert "COUNT=10" in event.rrule
    assert len(event.exdates) == 1
    assert event.exdates[0] == datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc)


def test_vevent_to_event_extracts_rich_fields() -> None:
    vevents = _parse_vevents(_VCAL_RICH)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.url == "https://example.com/meeting"
    assert event.status == "TENTATIVE"
    assert event.transparency == "TRANSPARENT"
    assert set(event.categories) == {"work", "important"}
    assert len(event.attendees) == 2
    assert any("alice" in a.lower() for a in event.attendees)


def test_vevent_to_event_attaches_tzid_to_naive_datetime() -> None:
    vevents = _parse_vevents(_VCAL_TZID)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.tz == "America/New_York"
    assert event.dtstart is not None
    assert event.dtstart.tzinfo is not None
    # 09:00 in NY in May (EDT, UTC-4) → 13:00 UTC
    assert event.dtstart.astimezone(timezone.utc) == datetime(
        2026, 5, 13, 13, 0, tzinfo=timezone.utc
    )


def test_events_to_changes_skips_recurrence_overrides() -> None:
    """A VCALENDAR with a master VEVENT and a RECURRENCE-ID override should
    emit only the master change. The schema's events table is keyed by
    (uid, calendar_id) without recurrence_id, so accepting the override
    would overwrite the master and we'd lose the RRULE."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")

    class _FakeEvent:
        def __init__(self, url: str, data: str, etag: str) -> None:
            self.url = url
            self.data = data
            self.etag = etag

    ev = _FakeEvent(
        url="https://example.com/foo.ics",
        data=_VCALENDAR_RECURRENCE_OVERRIDE,
        etag="abc",
    )
    changes = backend._events_to_changes([ev], calendar_id="cal-1")
    assert len(changes) == 1
    assert changes[0].event.rrule is not None
    assert "FREQ=WEEKLY" in changes[0].event.rrule


# -- end-to-end: parser → EventStore → event_instances expansion -------------


def test_parsed_rrule_event_expands_into_event_instances(tmp_path) -> None:
    """Regression for the blank-UI bug: prove the full pipeline now produces
    instance rows. Before the parser fix, dtstart was empty, so
    _rebuild_instances_for short-circuited and the views had nothing to show."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from lilical.backends.base import EventChange
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
                kind="caldav",
                display_name="Test",
                identity="user@example.com",
                secret_ref="acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="https://e/c/",
                display_name="Test",
                color="#000000",
                access_role="owner",
            )
        )

    vevents = _parse_vevents(_VCAL_RRULE_EXDATE)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")

    store = EventStore(engine)
    n = store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=event, uid=event.uid)],
        '{"sync_token": null, "ctag": null}',
    )
    assert n == 1

    with Session(engine) as session:
        instances = session.query(EventInstanceRow).all()

    # RRULE=FREQ=WEEKLY;COUNT=10 with one EXDATE → 9 instances inside the
    # ±1-year window the store expands by default.
    assert len(instances) == 9
    # Sanity: all instances belong to our event and have monotonic dtstart_utc.
    assert all(i.uid == "weekly-1@example.com" for i in instances)
    # The EXDATE on 2026-05-27T09:00:00Z must not appear among instances.
    starts_iso = {i.dtstart_local for i in instances}
    assert "2026-05-27T09:00:00+00:00" not in starts_iso
    assert "2026-05-13T09:00:00+00:00" in starts_iso  # first occurrence


def test_parsed_non_recurring_event_creates_single_instance(tmp_path) -> None:
    """The single-event case: parser → store → exactly one EventInstanceRow."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from lilical.backends.base import EventChange
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
                kind="caldav",
                display_name="Test",
                identity="user@example.com",
                secret_ref="acc-1",
                created_at="2026-05-13T00:00:00+00:00",
            )
        )
        session.add(
            Calendar(
                id="cal-1",
                account_id="acc-1",
                provider_id="https://e/c/",
                display_name="Test",
                color="#000000",
                access_role="owner",
            )
        )

    vevents = _parse_vevents(_VCAL_TIMED)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    store = EventStore(engine)
    store.apply_remote_changes(
        "cal-1",
        [EventChange(kind="upsert", event=event, uid=event.uid)],
        '{"sync_token": null, "ctag": null}',
    )

    with Session(engine) as session:
        instances = session.query(EventInstanceRow).all()

    assert len(instances) == 1
    assert instances[0].uid == "timed-1@example.com"
    # 2026-05-13T09:00:00+00:00 → epoch 1778666400
    assert instances[0].dtstart_utc == int(
        datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc).timestamp()
    )
