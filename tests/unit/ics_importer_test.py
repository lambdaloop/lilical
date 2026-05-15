from __future__ import annotations

from pathlib import Path

import pytest

from lilical.ics.importer import parse_ics_file

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ics"


def test_parse_single_vevent() -> None:
    events = parse_ics_file(FIXTURES / "single_vevent.ics")
    assert len(events) == 1
    assert events[0].uid == "single-event-1@example.com"
    assert events[0].summary == "Team meeting"
    assert events[0].calendar_id == ""


def test_parse_multiple_vevents() -> None:
    events = parse_ics_file(FIXTURES / "multi_vevent.ics")
    assert len(events) == 2
    uids = {e.uid for e in events}
    assert "multi-event-1@example.com" in uids
    assert "multi-event-2@example.com" in uids
    summaries = {e.summary for e in events}
    assert "First meeting" in summaries
    assert "Second meeting" in summaries


def test_parse_event_with_no_uid_yields_empty_uid() -> None:
    events = parse_ics_file(FIXTURES / "no_uid.ics")
    assert len(events) == 1
    assert events[0].uid == ""
    assert events[0].summary == "Event without UID"


def test_parse_missing_summary_yields_empty_string(tmp_path: Path) -> None:
    ics = tmp_path / "nosummary.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        "UID:no-summary@example.com\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20260613T090000Z\nDTEND:20260613T100000Z\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    events = parse_ics_file(ics)
    assert len(events) == 1
    assert events[0].summary == ""


def test_parse_no_vevent_returns_empty_list(tmp_path: Path) -> None:
    ics = tmp_path / "empty.ics"
    ics.write_text("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")
    events = parse_ics_file(ics)
    assert events == []


def test_parse_nonexistent_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        parse_ics_file("/nonexistent/path/event.ics")
