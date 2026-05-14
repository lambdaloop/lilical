from __future__ import annotations

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
    backend = CalDavBackend(
        "acc-1", "https://wrong-host.example", "u", "p"
    )
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
        return httpx.Response(
            307, headers={"Location": "https://snailbox.ink/dav/cal"}
        )

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
