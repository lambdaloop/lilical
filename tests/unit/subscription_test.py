"""Tests for the ICS subscription backend, cursor, parser, and fetcher."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from lilical.backends.base import EventChange, PermanentError
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
    # Pretend existing local DB has uids {a, b}.
    backend._list_event_uids = lambda cal_id: {"a@x", "b@x"}  # type: ignore[method-assign]

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
