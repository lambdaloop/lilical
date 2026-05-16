from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx
import icalendar
import pytest

from lilical.backends.base import CursorExpired, PermanentError
from lilical.backends.caldav import (
    CalDavBackend,
    CalDavCursor,
    _discover_caldav_url,
    _parse_vevents,
    _vevent_to_event,
)


async def _aresult(value):
    return value


# -- list_calendars: PROPFIND-based discovery with inlined color ---------------


@dataclass
class _FakePropfindItem:
    href: str
    properties: dict = field(default_factory=dict)


class _FakePropfindResponse:
    def __init__(self, results: list) -> None:
        self.results = results


class _FakePrincipal:
    def __init__(self, url: str = "https://example.com/principals/u/") -> None:
        self.url = url


_CAL_RESOURCE_TYPE = ["{urn:ietf:params:xml:ns:caldav}calendar", "{DAV:}collection"]


class _FakeClient:
    def __init__(
        self,
        principal_result,
        home_url: str = "https://example.com/calendars/",
        cal_items: list[_FakePropfindItem] | None = None,
    ) -> None:
        self._principal_result = principal_result
        self._home_url = home_url
        self._cal_items = cal_items or []
        self.CALENDAR_LIST_PROPS = [
            "{DAV:}resourcetype",
            "{DAV:}displayname",
            "{http://apple.com/ns/ical/}calendar-color",
        ]

    def principal(self):
        if isinstance(self._principal_result, Exception):
            raise self._principal_result
        return self._principal_result

    def propfind(self, url: str, props, depth: int = 0):
        if depth == 0:
            # home-set lookup
            return _FakePropfindResponse(
                [
                    _FakePropfindItem(
                        href=str(self._principal_result.url)
                        if not isinstance(self._principal_result, Exception)
                        else "",
                        properties={
                            "{urn:ietf:params:xml:ns:caldav}calendar-home-set": (
                                self._home_url
                            )
                        },
                    )
                ]
            )
        # depth=1: calendar list
        return _FakePropfindResponse(self._cal_items)

    def _make_absolute_url(self, url: str) -> str:
        if url and not url.startswith("http"):
            return "https://example.com" + url
        return url


def _wire_fake_client(backend: CalDavBackend, client: _FakeClient) -> None:
    backend._get_client = lambda: _aresult(client)  # type: ignore[method-assign]
    # _run normally goes through asyncio.to_thread; in tests we run inline.
    backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_list_calendars_converts_url_objects_to_strings() -> None:
    """list_calendars must return str values for id/provider_id/display_name."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    cal_item = _FakePropfindItem(
        href="https://example.com/calendars/work/",
        properties={
            "{DAV:}resourcetype": _CAL_RESOURCE_TYPE,
            "{DAV:}displayname": "Work",
        },
    )
    _wire_fake_client(
        backend,
        _FakeClient(_FakePrincipal(), cal_items=[cal_item]),
    )

    result = await backend.list_calendars()

    assert len(result) == 1
    entry = result[0]
    assert isinstance(entry["id"], str)
    assert isinstance(entry["provider_id"], str)
    assert isinstance(entry["display_name"], str)
    assert entry["provider_id"] == "https://example.com/calendars/work/"


@pytest.mark.asyncio
async def test_list_calendars_falls_back_to_id_when_name_missing() -> None:
    """When displayname is absent, fall back to the last URL path segment."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    cal_item = _FakePropfindItem(
        href="https://example.com/calendars/personal/",
        properties={"{DAV:}resourcetype": _CAL_RESOURCE_TYPE},
    )
    _wire_fake_client(
        backend,
        _FakeClient(_FakePrincipal(), cal_items=[cal_item]),
    )

    result = await backend.list_calendars()
    assert result[0]["display_name"] == "personal"


@pytest.mark.asyncio
async def test_list_calendars_extracts_color_without_extra_request() -> None:
    """calendar-color extracted from same depth-1 PROPFIND, not a per-cal request."""
    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
    cal_item = _FakePropfindItem(
        href="https://example.com/calendars/work/",
        properties={
            "{DAV:}resourcetype": _CAL_RESOURCE_TYPE,
            "{DAV:}displayname": "Work",
            "{http://apple.com/ns/ical/}calendar-color": "#4A90D9FF",
        },
    )
    _wire_fake_client(
        backend,
        _FakeClient(_FakePrincipal(), cal_items=[cal_item]),
    )

    result = await backend.list_calendars()
    assert result[0]["color"] == "#4a90d9"


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


_VCAL_ALL_DAY_UTC_MIDNIGHT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:all-day-utc@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260704T000000Z
DTEND:20260705T000000Z
SUMMARY:Independence Day (UTC midnight style)
END:VEVENT
END:VCALENDAR
"""


_VCAL_ALL_DAY_NAIVE_DURATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:all-day-dur@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260704T000000
DURATION:P1D
SUMMARY:Independence Day (naive midnight + P1D)
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
    # All-day events are now stored as midnight in the local zone so that
    # .date() returns the right calendar day regardless of the runner's UTC offset.
    assert event.dtstart is not None
    assert event.dtstart.tzinfo is not None
    assert event.dtstart.date() == date(2026, 7, 4)
    assert event.dtend is not None
    assert event.dtend.date() == date(2026, 7, 5)


def test_vevent_to_event_detects_all_day_from_utc_midnight() -> None:
    """Radicale/Baikal-style: DTSTART:YYYYMMDDT000000Z with no VALUE=DATE."""
    vevents = _parse_vevents(_VCAL_ALL_DAY_UTC_MIDNIGHT)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.all_day is True
    assert event.dtstart is not None
    assert event.dtstart.tzinfo is not None
    assert event.dtstart.date() == date(2026, 7, 4)
    assert event.dtend is not None
    assert event.dtend.date() == date(2026, 7, 5)


def test_vevent_to_event_detects_all_day_from_naive_midnight_with_duration() -> None:
    """Naive midnight DTSTART + DURATION:P1D should be detected as all-day."""
    vevents = _parse_vevents(_VCAL_ALL_DAY_NAIVE_DURATION)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.all_day is True
    assert event.dtstart is not None
    assert event.dtstart.tzinfo is not None
    assert event.dtstart.date() == date(2026, 7, 4)
    assert event.dtend is not None
    assert event.dtend.date() == date(2026, 7, 5)


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
    assert any("alice" in a.email for a in event.attendees)


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


def test_events_to_changes_emits_master_and_override() -> None:
    """A VCALENDAR with a master VEVENT and a RECURRENCE-ID override should
    emit two changes: one for the master (with rrule) and one for the override
    (with recurrence_id set, rrule=None)."""
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
    assert len(changes) == 2
    by_kind = {c.event.recurrence_id is None: c for c in changes}
    master = by_kind[True]
    override = by_kind[False]
    assert master.event.rrule is not None
    assert "FREQ=WEEKLY" in master.event.rrule
    assert override.event.recurrence_id is not None
    assert override.event.rrule is None


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
    # Compare in UTC so the test passes regardless of the runner's local tz.
    # dtstart_local is re-localized to the local zone, so we parse + convert.
    from datetime import timezone as _tz

    starts_utc = {
        datetime.fromisoformat(i.dtstart_local).astimezone(_tz.utc) for i in instances
    }
    exdate_utc = datetime(2026, 5, 27, 9, 0, 0, tzinfo=_tz.utc)
    first_utc = datetime(2026, 5, 13, 9, 0, 0, tzinfo=_tz.utc)
    # The EXDATE on 2026-05-27T09:00:00Z must not appear among instances.
    assert exdate_utc not in starts_utc
    assert first_utc in starts_utc  # first occurrence


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


# -- sync-collection REPORT (RFC 6578) ----------------------------------------

_VEVENT_ICAL = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:test-uid-1@example.com
SUMMARY:Sync event
DTSTART:20260514T100000Z
DTEND:20260514T110000Z
END:VEVENT
END:VCALENDAR
"""


class _UrlLike2:
    def __init__(self, value: str) -> None:
        self._v = value

    def __str__(self) -> str:
        return self._v

    def rstrip(self, chars: str) -> str:
        return self._v.rstrip(chars)

    def rsplit(self, sep: str, maxsplit: int = -1) -> list[str]:
        return self._v.rsplit(sep, maxsplit)


class _FakeSyncObj:
    def __init__(self, url: str, data: str | None, etag: str = '"e1"') -> None:
        self.url = _UrlLike2(url)
        self.data = data
        self.etag = etag


class _FakeSyncResult:
    def __init__(self, objects: list, sync_token: str) -> None:
        self.objects = objects
        self.sync_token = sync_token

    def __iter__(self):
        return iter(self.objects)


def _wire_fake_client2(
    backend: CalDavBackend,
    get_objects_by_sync_token_fn,
    search_fn=None,
    get_properties_fn=None,
):
    """Wire a backend so _get_client returns a fake, and _run dispatches to lambdas."""
    import caldav as _caldav

    class _FakeCal2:
        def __init__(self, url):
            self.url = url

        def get_objects_by_sync_token(self, **kwargs):
            return get_objects_by_sync_token_fn(**kwargs)

        def search(self, **kwargs):
            return search_fn(**kwargs) if search_fn else []

        def get_properties(self, props):
            return get_properties_fn(props) if get_properties_fn else {}

    class _FakeClient2:
        pass

    fake_client = _FakeClient2()
    fake_cal = _FakeCal2(url="https://example.com/cal/1/")

    backend._get_client = lambda: _aresult(fake_client)  # type: ignore[method-assign]
    original_caldav_calendar = _caldav.Calendar

    def _patched_calendar(client, url):
        return fake_cal

    _caldav.Calendar = _patched_calendar  # type: ignore[attr-defined]
    backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]
    return _caldav, original_caldav_calendar


@pytest.mark.asyncio
async def test_incremental_sync_uses_sync_collection_when_token_available() -> None:
    """incremental_sync calls get_objects_by_sync_token when cursor has a real token."""
    import caldav as _caldav

    result = _FakeSyncResult(
        objects=[
            _FakeSyncObj("https://cal/1/test-uid-1@example.com.ics", _VEVENT_ICAL)
        ],
        sync_token="http://example.com/sync/token/v2",
    )
    called_with: list[dict] = []

    def get_objects_by_sync_token(**kwargs):
        called_with.append(kwargs)
        return result

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _wire_fake_client2(backend, get_objects_by_sync_token)

        cursor = CalDavCursor(sync_token="http://example.com/sync/token/v1")
        changes, new_cursor = await backend.incremental_sync("https://cal/1/", cursor)
    finally:
        _caldav.Calendar = original

    assert len(called_with) == 1
    assert called_with[0]["sync_token"] == "http://example.com/sync/token/v1"
    assert called_with[0]["load_objects"] is True
    assert called_with[0]["disable_fallback"] is True
    assert len(changes) == 1
    assert changes[0].uid == "test-uid-1@example.com"
    assert new_cursor.to_json()["sync_token"] == "http://example.com/sync/token/v2"


@pytest.mark.asyncio
async def test_incremental_sync_maps_report_error_to_cursor_expired() -> None:
    """A ReportError from get_objects_by_sync_token (stale token) → CursorExpired."""
    import caldav as _caldav
    from caldav.lib.error import ReportError as _ReportError

    def get_objects_by_sync_token(**kwargs):
        raise _ReportError("Invalid sync token")

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _wire_fake_client2(backend, get_objects_by_sync_token)

        cursor = CalDavCursor(sync_token="stale-token")
        with pytest.raises(CursorExpired):
            await backend.incremental_sync("https://cal/1/", cursor)
    finally:
        _caldav.Calendar = original


@pytest.mark.asyncio
async def test_incremental_sync_falls_back_without_token() -> None:
    """With no sync token, incremental_sync falls back to date-windowed REPORT."""
    import caldav as _caldav

    class _FakeEv:
        url = _UrlLike2("https://cal/1/test-uid-1@example.com.ics")
        data = _VEVENT_ICAL
        etag = '"e1"'

    searched: list = []

    def search(**kwargs):
        searched.append(kwargs)
        return [_FakeEv()]

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")

        class _FakeCal:
            url = "https://cal/1/"

            def get_objects_by_sync_token(self, **kw):
                raise AssertionError("should not be called")  # noqa: E704

            def search(self, **kw):
                return search(**kw)

            def get_properties(self, p):
                return {}

        _caldav.Calendar = lambda client, url: _FakeCal()  # type: ignore[attr-defined]
        backend._get_client = lambda: _aresult(object())  # type: ignore[method-assign]
        backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]

        cursor = CalDavCursor(sync_token=None)
        changes, new_cursor = await backend.incremental_sync("https://cal/1/", cursor)
    finally:
        _caldav.Calendar = original

    assert len(searched) == 1
    assert len(changes) == 1


@pytest.mark.asyncio
async def test_incremental_sync_sync_result_handles_deletes() -> None:
    """Deleted objects (data=None) produce EventChange(kind='delete')."""
    import caldav as _caldav

    result = _FakeSyncResult(
        objects=[
            _FakeSyncObj("https://cal/1/gone-uid.ics", data=None),
        ],
        sync_token="tok-new",
    )

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _wire_fake_client2(backend, lambda **kw: result)

        cursor = CalDavCursor(sync_token="tok-old")
        changes, _ = await backend.incremental_sync("https://cal/1/", cursor)
    finally:
        _caldav.Calendar = original

    assert len(changes) == 1
    assert changes[0].kind == "delete"
    assert changes[0].uid == "gone-uid"


@pytest.mark.asyncio
async def test_initial_sync_returns_sync_token_in_cursor() -> None:
    """initial_sync must capture the server's sync-token and store it in the cursor."""
    import caldav as _caldav
    from caldav.elements import dav as _dav

    class _FakeEv:
        url = _UrlLike2("https://cal/1/ev.ics")
        data = _VEVENT_ICAL
        etag = '"e1"'

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")

        class _FakeCal:
            url = "https://cal/1/"

            def search(self, **kw):
                return [_FakeEv()]

            def get_properties(self, props):
                return {_dav.SyncToken.tag: "http://example.com/token/1"}

        _caldav.Calendar = lambda client, url: _FakeCal()  # type: ignore[attr-defined]
        backend._get_client = lambda: _aresult(object())  # type: ignore[method-assign]
        backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]

        cursors = []
        async for _changes, cursor in backend.initial_sync("https://cal/1/"):
            cursors.append(cursor)
    finally:
        _caldav.Calendar = original

    assert len(cursors) == 1
    assert cursors[0].to_json()["sync_token"] == "http://example.com/token/1"


# ── create_event ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_event_builds_vevent_and_calls_save_event() -> None:
    """create_event calls cal_obj.save_event with VEVENT data, returns the event."""
    import caldav as _caldav

    from lilical.models.event import Event

    saved: list[str] = []

    class _FakeCal:
        url = "https://cal/1/"

        def save_event(self, data: str) -> None:
            saved.append(data)

    original = _caldav.Calendar
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _caldav.Calendar = lambda client, url: _FakeCal()  # type: ignore[attr-defined]
        backend._get_client = lambda: _aresult(object())  # type: ignore[method-assign]
        backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]

        event = Event(
            uid="test-uid",
            calendar_id="https://cal/1/",
            summary="My meeting",
            dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
        )
        returned = await backend.create_event("https://cal/1/", event)
    finally:
        _caldav.Calendar = original

    assert len(saved) == 1
    assert "BEGIN:VEVENT" in saved[0]
    assert "test-uid" in saved[0]
    assert "My meeting" in saved[0]
    # create_event returns a new Event with provider_event_id set (not identity-equal)
    assert returned.uid == event.uid
    assert returned.summary == event.summary
    assert returned.provider_event_id is not None


# ── _normalise_hex_color matrix ───────────────────────────────────────────────


def test_normalise_hex_color_none_returns_none() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    assert _normalise_hex_color(None) is None


def test_normalise_hex_color_empty_returns_none() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    assert _normalise_hex_color("") is None


def test_normalise_hex_color_no_hash_returns_none() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    assert _normalise_hex_color("ff0000") is None


def test_normalise_hex_color_seven_char_lowercased() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    assert _normalise_hex_color("#AABBCC") == "#aabbcc"
    assert _normalise_hex_color("#aabbcc") == "#aabbcc"


def test_normalise_hex_color_eight_char_strips_alpha() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    # Apple sends #RRGGBBAA; we keep only #RRGGBB
    assert _normalise_hex_color("#FF0000FF") == "#ff0000"


def test_normalise_hex_color_three_char_expands() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    result = _normalise_hex_color("#F0A")
    assert result is not None
    assert result.lower() == "#ff00aa"


def test_normalise_hex_color_invalid_length_returns_none() -> None:
    from lilical.backends.caldav import _normalise_hex_color

    assert _normalise_hex_color("#12345") is None  # 6-char string but only 5 hex digits
    assert _normalise_hex_color("#1234567890") is None


# ── error classification: AuthorizationError / DAVError 401 / 403 ─────────────


@pytest.mark.asyncio
async def test_authorization_error_maps_to_auth_expired() -> None:
    from caldav.lib.error import AuthorizationError

    from lilical.backends.base import AuthExpired

    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")

    async def _raise_auth(_):
        raise AuthorizationError("401 Unauthorized")

    backend._get_client = lambda: _raise_auth(None)  # type: ignore[method-assign]

    with pytest.raises(AuthExpired):
        await backend.list_calendars()


@pytest.mark.asyncio
async def test_dav_error_401_maps_to_auth_expired() -> None:
    from caldav.lib.error import DAVError

    from lilical.backends.base import AuthExpired

    backend = CalDavBackend("acc-1", "https://example.com", "u", "p")

    async def _raise_dav(_):
        e = DAVError("401 error")
        e.url = "HTTP/1.1 401 Unauthorized"  # type: ignore[attr-defined]
        raise e

    backend._get_client = lambda: _raise_dav(None)  # type: ignore[method-assign]

    with pytest.raises(AuthExpired):
        await backend.list_calendars()


# ── recurring: RDATE read path ────────────────────────────────────────────────


def test_vevent_to_event_extracts_rdate() -> None:
    """RDATE lines in a VEVENT should be parsed into event.rdates."""
    vcal = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:rdate-test@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260513T090000Z
DTEND:20260513T100000Z
RRULE:FREQ=WEEKLY;COUNT=2
RDATE:20260120T100000Z
SUMMARY:Meeting with extra date
END:VEVENT
END:VCALENDAR
"""
    vevents = _parse_vevents(vcal)
    event = _vevent_to_event(vevents[0], calendar_id="cal-1", href="h", etag="e")
    assert event.rdates is not None
    assert len(event.rdates) == 1
    assert event.rdates[0] == datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)


# ── _ical_serializer: VCALENDAR output assertions ─────────────────────────────


def test_event_to_vcalendar_includes_rrule() -> None:
    """Recurring events must have RRULE in the serialized iCal output."""
    from lilical.backends._ical_serializer import event_to_vcalendar
    from lilical.models.event import Event

    event = Event(
        uid="uid-weekly",
        calendar_id="cal-1",
        summary="Standup",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
        rrule="FREQ=WEEKLY;BYDAY=MO,WE",
    )
    ical = event_to_vcalendar(event).to_ical().decode()
    assert "RRULE:" in ical
    assert "FREQ=WEEKLY" in ical
    assert "BYDAY=MO,WE" in ical or "BYDAY=WE,MO" in ical


def test_event_to_vcalendar_all_day_uses_date_value() -> None:
    """All-day events must serialize DTSTART as DATE (;VALUE=DATE:YYYYMMDD),
    not a DATETIME, so CalDAV servers treat them as all-day correctly."""
    from lilical.backends._ical_serializer import event_to_vcalendar
    from lilical.models.event import Event

    event = Event(
        uid="uid-allday",
        calendar_id="cal-1",
        summary="Holiday",
        dtstart=datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc),
        all_day=True,
    )
    ical = event_to_vcalendar(event).to_ical().decode()
    # The DTSTART line must carry a DATE value, not a DATETIME with a 'T' separator.
    dtstart_line = next(
        (line for line in ical.splitlines() if line.startswith("DTSTART")), ""
    )
    assert dtstart_line, "DTSTART not found in serialized output"
    # The value part (after ":") must be a date-only string like "20260704",
    # not a datetime like "20260704T000000Z".
    value_part = dtstart_line.split(":")[-1].strip()
    assert "T" not in value_part, f"Expected DATE, got DATETIME: {dtstart_line!r}"
    assert "20260704" in value_part


def test_event_to_vcalendar_includes_tzid_for_non_utc() -> None:
    """Non-UTC events must include TZID= on the DTSTART/DTEND property."""
    from zoneinfo import ZoneInfo

    from lilical.backends._ical_serializer import event_to_vcalendar
    from lilical.models.event import Event

    ny_tz = ZoneInfo("America/New_York")
    # Pass a datetime whose tzinfo is genuinely NY (not UTC) — the serializer
    # checks `dt.tzinfo is timezone.utc` before choosing the TZID path.
    ny_dt = datetime(2026, 5, 13, 9, 0, tzinfo=ny_tz)
    event = Event(
        uid="uid-tz",
        calendar_id="cal-1",
        summary="NY Meeting",
        dtstart=ny_dt,
        dtend=ny_dt,
        tz="America/New_York",
    )
    ical = event_to_vcalendar(event).to_ical().decode()
    assert "TZID=America/New_York" in ical


def test_event_to_vcalendar_sequence_bump() -> None:
    """sequence_bump=True must increment SEQUENCE by 1."""
    from lilical.backends._ical_serializer import event_to_vcalendar
    from lilical.models.event import Event

    event = Event(
        uid="uid-seq",
        calendar_id="cal-1",
        summary="x",
        sequence=3,
    )
    ical_no_bump = event_to_vcalendar(event, sequence_bump=False).to_ical().decode()
    ical_bumped = event_to_vcalendar(event, sequence_bump=True).to_ical().decode()

    seq_line_no = next(
        line for line in ical_no_bump.splitlines() if line.startswith("SEQUENCE")
    )
    seq_line_bump = next(
        line for line in ical_bumped.splitlines() if line.startswith("SEQUENCE")
    )
    assert seq_line_no.split(":")[-1].strip() == "3"
    assert seq_line_bump.split(":")[-1].strip() == "4"


# ── delete_event: must use provider_event_id as the resource URL ──────────────


@pytest.mark.asyncio
async def test_delete_event_uses_provider_event_id_as_url() -> None:
    """delete_event must DELETE the resource at provider_event_id, not the uid."""
    import caldav as _caldav

    deleted_urls: list[str] = []

    class _FakeResource:
        def __init__(self, url: str) -> None:
            self._url = url

        def delete(self) -> None:
            deleted_urls.append(self._url)

    original = _caldav.CalendarObjectResource
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _caldav.CalendarObjectResource = lambda client, url: _FakeResource(url)  # type: ignore[attr-defined]
        backend._get_client = lambda: _aresult(object())  # type: ignore[method-assign]
        backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]

        await backend.delete_event(
            "https://cal/1/",
            "https://cal/1/specific-uid.ics",
            if_match=None,
        )
    finally:
        _caldav.CalendarObjectResource = original

    assert deleted_urls == ["https://cal/1/specific-uid.ics"]


@pytest.mark.asyncio
async def test_update_event_saves_and_returns_etag() -> None:
    """update_event must call save() (not just set_data) and return the new etag."""
    import caldav as _caldav

    from lilical.models.event import Event

    fake_etag = '"new-etag-value"'

    class _FakeResource:
        def __init__(self) -> None:
            self.data = ""
            self.etag = None

        def save(self) -> None:
            self.etag = fake_etag

    original = _caldav.CalendarObjectResource
    try:
        backend = CalDavBackend("acc-1", "https://example.com", "u", "p")
        _caldav.CalendarObjectResource = lambda client, url: _FakeResource()  # type: ignore[attr-defined]
        backend._get_client = lambda: _aresult(object())  # type: ignore[method-assign]
        backend._run = lambda fn, *a, **kw: _aresult(fn(*a, **kw))  # type: ignore[method-assign]

        event = Event(
            uid="test-upd",
            calendar_id="cal-1",
            provider_event_id="https://cal/1/test-upd.ics",
            summary="Updated event",
            dtstart=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            dtend=datetime(2026, 5, 14, 11, 0, tzinfo=timezone.utc),
        )
        result = await backend.update_event("cal-1", event, if_match='"old-etag"')
    finally:
        _caldav.CalendarObjectResource = original

    assert result.etag == fake_etag
    # The uid, calendar_id, etc. are preserved from the input event
    assert result.uid == "test-upd"
