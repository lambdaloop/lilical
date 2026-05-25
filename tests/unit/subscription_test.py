"""Tests for the ICS subscription backend, cursor, parser, and fetcher."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from lilical.backends.base import EventChange, PermanentError, TransientError
from lilical.backends.subscription import SubscriptionBackend, SubscriptionCursor
from lilical.ics.fetch import canonicalize_source, fetch_ics
from lilical.ics.importer import parse_ics_to_events
from lilical.models.event import Event
from lilical.sync.cursor import cursor_from_json, cursor_to_json

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ics"


# ── parser ────────────────────────────────────────────────────────────────────


def test_parse_returns_events_and_calname() -> None:
    body = (FIXTURES / "single_vevent.ics").read_bytes()
    events, calname = parse_ics_to_events(body, calendar_id="cal-1")
    assert len(events) == 1
    assert events[0].uid == "single-event-1@example.com"
    assert events[0].calendar_id == "cal-1"
    # This fixture has no X-WR-CALNAME, so calname is None.
    assert calname is None


def test_parse_extracts_x_wr_calname(tmp_path: Path) -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:My Feed\r\n"
        b"BEGIN:VEVENT\r\nUID:e1@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:e1\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    _events, calname = parse_ics_to_events(body, calendar_id="c")
    assert calname == "My Feed"


def test_parse_rrule_and_exdate() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:rec@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        b"SUMMARY:Daily\r\nRRULE:FREQ=DAILY;COUNT=5\r\n"
        b"EXDATE:20260603T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 1
    assert events[0].rrule is not None
    assert "FREQ=DAILY" in events[0].rrule
    assert len(events[0].exdates) == 1


def test_parse_all_day_event(tmp_path: Path) -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:allday@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;VALUE=DATE:20260615\r\nDTEND;VALUE=DATE:20260616\r\n"
        b"SUMMARY:Holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 1
    assert events[0].all_day is True


def test_parse_recurrence_id_override() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:master@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        b"SUMMARY:Master\r\nRRULE:FREQ=DAILY;COUNT=3\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:master@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"RECURRENCE-ID:20260602T090000Z\r\n"
        b"DTSTART:20260602T140000Z\r\nDTEND:20260602T150000Z\r\n"
        b"SUMMARY:Override\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 2
    override = next(e for e in events if e.recurrence_id is not None)
    assert override.summary == "Override"


def test_parse_skips_vevent_with_no_uid() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        b"SUMMARY:Nameless\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert events == []


# ── cursor ────────────────────────────────────────────────────────────────────


def test_subscription_cursor_roundtrip() -> None:
    c = SubscriptionCursor(
        etag='W/"abc"',
        last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        content_sha256="deadbeef",
    )
    restored = cursor_from_json(cursor_to_json(c))
    assert isinstance(restored, SubscriptionCursor)
    assert restored.etag == c.etag
    assert restored.last_modified == c.last_modified
    assert restored.content_sha256 == c.content_sha256


def test_subscription_cursor_rejects_wrong_type() -> None:
    with pytest.raises(ValueError):
        SubscriptionCursor.from_json({"_type": "google", "sync_token": "x"})


# ── fetcher ───────────────────────────────────────────────────────────────────


def test_canonicalize_webcal_rewrites_to_https() -> None:
    assert canonicalize_source("webcal://example.com/feed.ics") == (
        "https://example.com/feed.ics"
    )


def test_canonicalize_passthrough_https() -> None:
    assert canonicalize_source("https://x/y.ics") == "https://x/y.ics"


def test_canonicalize_bare_path_becomes_file_url(tmp_path: Path) -> None:
    p = tmp_path / "cal.ics"
    p.write_text("x")
    assert canonicalize_source(str(p)) == f"file://{p}"


def test_canonicalize_empty_raises() -> None:
    with pytest.raises(ValueError):
        canonicalize_source("")


def test_fetch_file_returns_bytes_then_none_when_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "cal.ics"
    p.write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    src = f"file://{p}"

    body, _etag, lm = asyncio.run(fetch_ics(src))
    assert body == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    assert lm is not None

    # Same mtime → returns None (unchanged).
    body2, _e2, lm2 = asyncio.run(fetch_ics(src, prev_last_modified=lm))
    assert body2 is None
    assert lm2 == lm


def test_fetch_file_missing_raises_permanent() -> None:
    with pytest.raises(PermanentError):
        asyncio.run(fetch_ics("file:///nonexistent/path/missing.ics"))


# ── backend integration ──────────────────────────────────────────────────────


def _store_with_calendar(provider_id: str, cal_id: str = "cal-sub") -> MagicMock:
    """Build a mock EventStore that returns one Calendar row for the subscription
    account, matching *provider_id*."""

    cal = MagicMock()
    cal.id = cal_id
    cal.provider_id = provider_id
    cal.display_name = "Test feed"
    cal.color = "#5e9fff"
    store = MagicMock()
    store.list_calendars.return_value = [cal]
    return store


def test_list_calendars_returns_store_rows(tmp_path: Path) -> None:
    p = tmp_path / "x.ics"
    p.write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    src = f"file://{p}"
    store = _store_with_calendar(src)
    backend = SubscriptionBackend(account_id="subscriptions", store=store)
    cals = asyncio.run(backend.list_calendars())
    assert cals == [
        {"provider_id": src, "display_name": "Test feed", "color": "#5e9fff"}
    ]


def test_initial_sync_yields_upserts(tmp_path: Path) -> None:
    body = (FIXTURES / "single_vevent.ics").read_bytes()
    p = tmp_path / "x.ics"
    p.write_bytes(body)
    src = f"file://{p}"
    store = _store_with_calendar(src)
    backend = SubscriptionBackend(account_id="subscriptions", store=store)

    async def _run() -> list[tuple[list[EventChange], Any]]:
        out: list[tuple[list[EventChange], Any]] = []
        async for batch in backend.initial_sync(src):
            out.append(batch)
        return out

    pages = asyncio.run(_run())
    assert len(pages) == 1
    changes, cursor = pages[0]
    assert len(changes) == 1
    assert changes[0].kind == "upsert"
    assert isinstance(cursor, SubscriptionCursor)
    assert cursor.content_sha256


def test_incremental_sync_emits_deletes_for_removed_uids(tmp_path: Path) -> None:
    src_body_v2 = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:a@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:A renamed\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:c@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T120000Z\r\nDTEND:20260613T130000Z\r\n"
        b"SUMMARY:C\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    p = tmp_path / "x.ics"
    p.write_bytes(src_body_v2)
    src = f"file://{p}"

    store = _store_with_calendar(src, cal_id="cal-sub")
    backend = SubscriptionBackend(account_id="subscriptions", store=store)
    # Pretend existing local DB has a@x (with a stale sig) and b@x.
    backend._list_event_signatures = lambda cal_id: {  # type: ignore[method-assign]
        ("a@x", ""): "stale-sig",
        ("b@x", ""): "stale-sig",
    }

    cursor = SubscriptionCursor(etag=None, last_modified=None, content_sha256="old")
    # Force mtime mismatch by passing a fake prev_last_modified.
    changes, new_cursor = asyncio.run(backend.incremental_sync(src, cursor))
    kinds = [(c.kind, c.uid) for c in changes]
    assert ("upsert", "a@x") in kinds
    assert ("upsert", "c@x") in kinds
    assert ("delete", "b@x") in kinds
    assert isinstance(new_cursor, SubscriptionCursor)
    assert new_cursor.content_sha256 != "old"


def test_incremental_sync_short_circuits_when_sha_matches(tmp_path: Path) -> None:
    body = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    p = tmp_path / "x.ics"
    p.write_bytes(body)
    src = f"file://{p}"
    import hashlib

    sha = hashlib.sha256(body).hexdigest()
    store = _store_with_calendar(src)
    backend = SubscriptionBackend(account_id="subscriptions", store=store)

    cursor = SubscriptionCursor(etag=None, last_modified=None, content_sha256=sha)
    changes, new_cursor = asyncio.run(backend.incremental_sync(src, cursor))
    assert changes == []
    assert isinstance(new_cursor, SubscriptionCursor)
    assert new_cursor.content_sha256 == sha


# ── read-only enforcement ────────────────────────────────────────────────────


def _make_event() -> Event:
    return Event(
        uid="x",
        calendar_id="c",
        dtstart=datetime(2026, 6, 1, 9, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
    )


def test_create_event_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.create_event("c", _make_event()))


def test_update_event_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.update_event("c", _make_event(), if_match=None))


def test_delete_event_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.delete_event("c", "pid", if_match=None))


def test_rename_calendar_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.rename_calendar("c", "new"))


def test_create_calendar_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.create_calendar("name"))


def test_delete_calendar_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.delete_calendar("c"))


def test_update_instance_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(
            backend.update_instance(
                "c",
                "pid",
                datetime(2026, 6, 1, 9, tzinfo=timezone.utc),
                _make_event(),
            )
        )


def test_delete_instance_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(
            backend.delete_instance(
                "c", "pid", datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
            )
        )


def test_respond_to_event_raises_permanent_error() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    with pytest.raises(PermanentError):
        asyncio.run(backend.respond_to_event("c", _make_event(), "ACCEPTED"))


def test_supported_contact_sources_is_empty() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    assert backend.supported_contact_sources() == ()


def test_list_contacts_returns_empty_done_page() -> None:
    backend = SubscriptionBackend(account_id="subscriptions", store=MagicMock())
    contacts, cursor, done = asyncio.run(backend.list_contacts("anything", None))
    assert contacts == []
    assert cursor is None
    assert done is True


# ── fetcher: HTTP code path (MockTransport) ──────────────────────────────────


def _patch_http(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Install a MockTransport for lilical.ics.fetch's httpx.AsyncClient.

    Returns a list that receives every Request the transport handles, so
    callers can assert on request headers.

    Captures the real AsyncClient before patching the module attribute —
    otherwise the factory would recurse into itself, since
    `lilical.ics.fetch.httpx.AsyncClient` is the same global as
    `httpx.AsyncClient`.
    """
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(**_kwargs):
        return real_async_client(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr("lilical.ics.fetch.httpx.AsyncClient", _factory)
    return seen


def test_fetch_http_200_returns_body_etag_last_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "ETag": 'W/"abc"',
                "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
            },
        )

    _patch_http(monkeypatch, handler)
    got_body, etag, last_modified = asyncio.run(fetch_ics("https://x/cal.ics"))
    assert got_body == body
    assert etag == 'W/"abc"'
    assert last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_fetch_http_304_returns_none_and_keeps_prev_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_http(monkeypatch, lambda _req: httpx.Response(304))
    body, etag, last_modified = asyncio.run(
        fetch_ics(
            "https://x/cal.ics",
            prev_etag='W/"abc"',
            prev_last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
    )
    assert body is None
    assert etag == 'W/"abc"'
    assert last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_fetch_http_404_raises_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, lambda _req: httpx.Response(404))
    with pytest.raises(PermanentError):
        asyncio.run(fetch_ics("https://x/cal.ics"))


def test_fetch_http_500_raises_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, lambda _req: httpx.Response(503))
    with pytest.raises(TransientError):
        asyncio.run(fetch_ics("https://x/cal.ics"))


def test_fetch_http_network_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    _patch_http(monkeypatch, boom)
    with pytest.raises(TransientError):
        asyncio.run(fetch_ics("https://x/cal.ics"))


def test_fetch_http_sends_if_none_match_when_prev_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(200, content=b""))
    asyncio.run(fetch_ics("https://x/cal.ics", prev_etag='W/"abc"'))
    assert seen and seen[0].headers.get("if-none-match") == 'W/"abc"'


def test_fetch_http_sends_if_modified_since_when_prev_last_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(200, content=b""))
    asyncio.run(
        fetch_ics(
            "https://x/cal.ics",
            prev_last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
    )
    assert (
        seen
        and seen[0].headers.get("if-modified-since") == "Wed, 21 Oct 2026 07:28:00 GMT"
    )


def test_fetch_http_omits_conditional_headers_when_no_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(200, content=b""))
    asyncio.run(fetch_ics("https://x/cal.ics"))
    assert seen
    assert "if-none-match" not in seen[0].headers
    assert "if-modified-since" not in seen[0].headers


def test_fetch_http_webcal_canonicalizes_to_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_http(monkeypatch, lambda _req: httpx.Response(200, content=b""))
    asyncio.run(fetch_ics("webcal://example.com/feed.ics"))
    assert seen and str(seen[0].url).startswith("https://example.com/")


def test_fetch_file_returns_bytes_when_mtime_changes(tmp_path: Path) -> None:
    p = tmp_path / "cal.ics"
    p.write_bytes(b"v1")
    src = f"file://{p}"

    _body, _etag, lm1 = asyncio.run(fetch_ics(src))
    # Bump mtime artificially: write new bytes and set a later mtime so the
    # second fetch sees a stale prev_last_modified hint.
    p.write_bytes(b"v2")
    later = _time.time() + 5
    os.utime(p, (later, later))

    body2, _e2, lm2 = asyncio.run(fetch_ics(src, prev_last_modified=lm1))
    assert body2 == b"v2"
    assert lm2 != lm1


# ── parser: branch coverage ──────────────────────────────────────────────────


def test_parse_event_with_tzid_keeps_source_zone() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:tz@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;TZID=America/New_York:20260613T090000\r\n"
        b"DTEND;TZID=America/New_York:20260613T100000\r\n"
        b"SUMMARY:NY meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 1
    e = events[0]
    assert e.tz == "America/New_York"
    assert e.dtstart is not None and e.dtstart.utcoffset() is not None
    # 09:00 NY local on 2026-06-13 is 13:00 UTC (EDT, UTC-4).
    assert e.dtstart.astimezone(timezone.utc).hour == 13


def test_parse_unknown_tzid_falls_back_to_utc() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:badtz@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;TZID=Not/A_Real_Zone:20260613T090000\r\n"
        b"DTEND;TZID=Not/A_Real_Zone:20260613T100000\r\n"
        b"SUMMARY:Bogus tz\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 1
    e = events[0]
    assert e.dtstart is not None and e.dtstart.tzinfo == timezone.utc


def test_parse_bare_utc_re_localized_to_local_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lilical.ics.importer.local_iana_tz", lambda: "America/Los_Angeles"
    )
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:utc@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T160000Z\r\nDTEND:20260613T170000Z\r\n"
        b"SUMMARY:UTC event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.tz == "America/Los_Angeles"
    # 16:00 UTC on 2026-06-13 = 09:00 PDT (UTC-7).
    assert e.dtstart is not None and e.dtstart.hour == 9


def test_parse_bare_utc_kept_when_local_is_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lilical.ics.importer.local_iana_tz", lambda: "UTC")
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:utc2@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T160000Z\r\nDTEND:20260613T170000Z\r\n"
        b"SUMMARY:UTC event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.tz == "UTC"
    assert e.dtstart is not None and e.dtstart.hour == 16


def test_parse_all_day_anchored_at_local_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lilical.ics.importer.local_iana_tz", lambda: "America/Los_Angeles"
    )
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:ad@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;VALUE=DATE:20260615\r\nDTEND;VALUE=DATE:20260616\r\n"
        b"SUMMARY:Holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.all_day is True
    assert e.tz == "America/Los_Angeles"
    assert e.dtstart is not None
    assert e.dtstart.hour == 0 and e.dtstart.minute == 0
    assert e.dtstart.date() == datetime(2026, 6, 15).date()


def test_parse_dtend_missing_with_duration_computes_end() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:dur@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDURATION:PT45M\r\n"
        b"SUMMARY:Short\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.dtstart is not None and e.dtend is not None
    assert (e.dtend - e.dtstart).total_seconds() == 45 * 60


def test_parse_dtend_missing_without_duration_falls_back_to_start() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:noend@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\n"
        b"SUMMARY:No end\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.dtend == e.dtstart


def test_parse_all_day_missing_dtend_defaults_to_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lilical.ics.importer.local_iana_tz", lambda: "UTC")
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:ad2@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;VALUE=DATE:20260615\r\n"
        b"SUMMARY:One day\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.all_day is True
    assert e.dtstart is not None and e.dtend is not None
    assert (e.dtend - e.dtstart).days == 1


def test_parse_categories_single_and_multi() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:cat@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"CATEGORIES:work,urgent\r\n"
        b"SUMMARY:cat\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert set(e.categories) == {"work", "urgent"}


def test_parse_populates_url_location_status_transparency_seq_lastmod() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:rich@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:Rich\r\nLOCATION:Room 3\r\n"
        b"URL:https://example.com/e\r\nSTATUS:TENTATIVE\r\n"
        b"TRANSP:TRANSPARENT\r\nSEQUENCE:7\r\n"
        b"LAST-MODIFIED:20260101T120000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    e = events[0]
    assert e.location == "Room 3"
    assert e.url == "https://example.com/e"
    assert e.status == "TENTATIVE"
    assert e.transparency == "TRANSPARENT"
    assert e.sequence == 7
    assert e.last_modified == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_malformed_sequence_defaults_to_zero() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:seq@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:Seq\r\nSEQUENCE:not-a-number\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert events[0].sequence == 0


def test_parse_vevent_missing_dtstart_is_skipped() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:nodt@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"SUMMARY:No DTSTART\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:ok@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:OK\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    uids = {e.uid for e in events}
    assert "nodt@x" not in uids
    assert "ok@x" in uids


def test_parse_non_utf8_bytes_do_not_crash() -> None:
    # Latin-1 high byte inside DESCRIPTION; decode("utf-8", errors="replace")
    # should swap it for U+FFFD without raising.
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:nonutf@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:caf\xe9\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events) == 1


def test_parse_empty_vcalendar_returns_empty_events_and_no_name() -> None:
    body = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    events, calname = parse_ics_to_events(body, calendar_id="c")
    assert events == []
    assert calname is None


def test_parse_rdate_with_multiple_values_flattens() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:rd@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260601T090000Z\r\nDTEND:20260601T100000Z\r\n"
        b"SUMMARY:RDate\r\n"
        b"RDATE:20260615T090000Z,20260622T090000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events, _ = parse_ics_to_events(body, calendar_id="c")
    assert len(events[0].rdates) == 2


# ── cursor: edge cases ───────────────────────────────────────────────────────


def test_subscription_cursor_from_json_missing_optional_fields() -> None:
    c = SubscriptionCursor.from_json({"_type": "subscription"})
    assert c.etag is None
    assert c.last_modified is None
    assert c.content_sha256 == ""


def test_subscription_cursor_roundtrip_with_none_etag_and_lm() -> None:
    c = SubscriptionCursor(etag=None, last_modified=None, content_sha256="sha")
    restored = cursor_from_json(cursor_to_json(c))
    assert isinstance(restored, SubscriptionCursor)
    assert restored.etag is None
    assert restored.last_modified is None
    assert restored.content_sha256 == "sha"


# ── backend round-out ────────────────────────────────────────────────────────


def test_incremental_sync_with_wrong_cursor_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "x.ics"
    p.write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    src = f"file://{p}"
    store = _store_with_calendar(src)
    backend = SubscriptionBackend(account_id="subscriptions", store=store)

    class _Other:
        def to_json(self) -> dict:
            return {"_type": "other"}

    with pytest.raises(PermanentError):
        asyncio.run(backend.incremental_sync(src, _Other()))  # type: ignore[arg-type]


def test_incremental_sync_short_circuits_when_body_none(tmp_path: Path) -> None:
    p = tmp_path / "x.ics"
    p.write_bytes(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    src = f"file://{p}"

    # Seed prev_last_modified from a first fetch so the mtime matches and the
    # fetcher returns body=None.
    _b, _e, lm = asyncio.run(fetch_ics(src))
    store = _store_with_calendar(src)
    backend = SubscriptionBackend(account_id="subscriptions", store=store)

    cursor = SubscriptionCursor(
        etag=None, last_modified=lm, content_sha256="prev-sha"
    )
    changes, new_cursor = asyncio.run(backend.incremental_sync(src, cursor))
    assert changes == []
    # body=None short-circuit preserves the prior content_sha256 so the diff
    # baseline doesn't drift.
    assert isinstance(new_cursor, SubscriptionCursor)
    assert new_cursor.content_sha256 == "prev-sha"


# ── backend integration against a real EventStore ────────────────────────────


def _real_store():
    """Build a real EventStore over in-memory SQLite. Schema DDL duplicated
    inline because tests aren't cross-importable in this project.

    Uses StaticPool so a single connection is shared across threads — the
    backend calls _lookup_local_cal_id / _list_event_uids via
    asyncio.to_thread, and each thread would otherwise get its own private
    in-memory database with an empty schema.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from lilical.storage.event_store import EventStore

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE accounts (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
            " display_name TEXT NOT NULL, identity TEXT NOT NULL,"
            " server_url TEXT, secret_ref TEXT NOT NULL,"
            " created_at TEXT NOT NULL, sort_order INTEGER DEFAULT 0,"
            " enabled INTEGER DEFAULT 1, include_contacts INTEGER DEFAULT 0)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE calendars (id TEXT PRIMARY KEY,"
            " account_id TEXT NOT NULL REFERENCES accounts(id),"
            " provider_id TEXT NOT NULL, display_name TEXT NOT NULL,"
            " color TEXT, is_primary INTEGER DEFAULT 0,"
            " is_visible INTEGER DEFAULT 1, is_included INTEGER DEFAULT 1,"
            " sort_order INTEGER DEFAULT 0, is_favorite INTEGER DEFAULT 0,"
            " access_role TEXT, sync_cursor TEXT, last_synced_at TEXT,"
            " UNIQUE(account_id, provider_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE events (uid TEXT NOT NULL,"
            " calendar_id TEXT NOT NULL REFERENCES calendars(id),"
            " recurrence_id TEXT NOT NULL DEFAULT '', provider_event_id TEXT,"
            " dtstart TEXT NOT NULL, dtend TEXT NOT NULL, tz TEXT NOT NULL,"
            " all_day INTEGER DEFAULT 0, summary TEXT DEFAULT '',"
            " description TEXT DEFAULT '', location TEXT DEFAULT '',"
            " url TEXT, rrule TEXT, exdates TEXT, rdates TEXT,"
            " attendees TEXT, organizer TEXT, categories TEXT, color TEXT,"
            " status TEXT DEFAULT 'CONFIRMED', self_response TEXT,"
            " transparency TEXT DEFAULT 'OPAQUE', valarms TEXT, etag TEXT,"
            " sequence INTEGER DEFAULT 0, last_modified TEXT,"
            " local_dirty INTEGER DEFAULT 0, deleted_locally INTEGER DEFAULT 0,"
            " conflict_state TEXT, local_modified_at TEXT, inserted_at TEXT,"
            " PRIMARY KEY(uid, calendar_id, recurrence_id),"
            " UNIQUE(calendar_id, provider_event_id, recurrence_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE pending_ops (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " account_id TEXT NOT NULL REFERENCES accounts(id),"
            " calendar_id TEXT NOT NULL REFERENCES calendars(id),"
            " uid TEXT NOT NULL, op TEXT NOT NULL, payload TEXT NOT NULL,"
            " if_match TEXT, attempts INTEGER DEFAULT 0,"
            " last_attempt_at TEXT, last_error TEXT,"
            " created_at TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE event_instances ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " uid TEXT NOT NULL, calendar_id TEXT NOT NULL,"
            " dtstart_utc INTEGER NOT NULL, dtend_utc INTEGER NOT NULL,"
            " dtstart_local TEXT NOT NULL, dtend_local TEXT NOT NULL,"
            " all_day INTEGER DEFAULT 0, is_override INTEGER DEFAULT 0,"
            " recurrence_id TEXT NOT NULL DEFAULT '')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE event_completions (calendar_id TEXT NOT NULL,"
            " uid TEXT NOT NULL, dtstart_utc INTEGER NOT NULL,"
            " completed_at TEXT NOT NULL,"
            " PRIMARY KEY(calendar_id, uid, dtstart_utc))"
        )
    return EventStore(engine)


def test_lookup_local_cal_id_returns_match_and_none(tmp_path: Path) -> None:
    body = (FIXTURES / "single_vevent.ics").read_bytes()
    p = tmp_path / "x.ics"
    p.write_bytes(body)
    src = f"file://{p}"

    store = _real_store()
    cal_id = store.create_subscription(
        canonical_source=src,
        display_name="Feed",
        color="#5e9fff",
        events=[],
        content_sha256="seed",
    )
    backend = SubscriptionBackend(account_id="subscriptions", store=store)
    assert backend._lookup_local_cal_id(src) == cal_id
    assert backend._lookup_local_cal_id("file:///nope") is None


def test_list_event_signatures_returns_real_uids(tmp_path: Path) -> None:
    body = (FIXTURES / "multi_vevent.ics").read_bytes()
    p = tmp_path / "x.ics"
    p.write_bytes(body)
    src = f"file://{p}"

    store = _real_store()
    parsed, _ = parse_ics_to_events(body, calendar_id="")
    cal_id = store.create_subscription(
        canonical_source=src,
        display_name="Feed",
        color="#5e9fff",
        events=parsed,
        content_sha256=hashlib.sha256(body).hexdigest(),
    )
    backend = SubscriptionBackend(account_id="subscriptions", store=store)
    sigs = backend._list_event_signatures(cal_id)
    assert {k[0] for k in sigs} == {e.uid for e in parsed}
    # Every signature is a 64-char SHA-256 hex digest.
    assert all(len(v) == 64 for v in sigs.values())


def test_incremental_sync_end_to_end_applies_to_store(tmp_path: Path) -> None:
    # Initial body: events {a, b}. Second body: events {a (renamed), c}.
    v1 = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:a@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:A\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:b@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T110000Z\r\nDTEND:20260613T120000Z\r\n"
        b"SUMMARY:B\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    v2 = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:a@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T090000Z\r\nDTEND:20260613T100000Z\r\n"
        b"SUMMARY:A renamed\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:c@x\r\nDTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260613T130000Z\r\nDTEND:20260613T140000Z\r\n"
        b"SUMMARY:C\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    p = tmp_path / "x.ics"
    p.write_bytes(v1)
    src = f"file://{p}"

    store = _real_store()
    parsed_v1, _ = parse_ics_to_events(v1, calendar_id="")
    cal_id = store.create_subscription(
        canonical_source=src,
        display_name="Feed",
        color="#5e9fff",
        events=parsed_v1,
        content_sha256=hashlib.sha256(v1).hexdigest(),
    )
    backend = SubscriptionBackend(account_id="subscriptions", store=store)

    # Rewrite + bump mtime so the file-fetch returns the new body.
    p.write_bytes(v2)
    later = _time.time() + 5
    os.utime(p, (later, later))

    prior = SubscriptionCursor(
        etag=None,
        last_modified=None,
        content_sha256=hashlib.sha256(v1).hexdigest(),
    )
    changes, new_cursor = asyncio.run(backend.incremental_sync(src, prior))
    import json

    store.apply_remote_changes(cal_id, changes, json.dumps(new_cursor.to_json()))

    uids = {k[0] for k in backend._list_event_signatures(cal_id)}
    assert "a@x" in uids
    assert "c@x" in uids
    assert "b@x" not in uids
