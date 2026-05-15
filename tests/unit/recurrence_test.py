from datetime import datetime, timezone

import pytest

from lilical.models.event import Event
from lilical.recurrence.expander import RecurrenceExpander
from lilical.storage.event_store import EventStore


class FakeEngine:
    pass


@pytest.fixture
def expander() -> RecurrenceExpander:
    store = EventStore(FakeEngine())
    return RecurrenceExpander(store)


def test_non_recurring_returns_single(expander: RecurrenceExpander) -> None:
    e = Event(
        uid="e1",
        calendar_id="cal-1",
        summary="Test",
        dtstart=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 12, 31, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 1


def test_daily_rrule(expander: RecurrenceExpander) -> None:
    e = Event(
        uid="e2",
        calendar_id="cal-1",
        summary="Daily standup",
        rrule="FREQ=DAILY;COUNT=3",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
    )
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 3


def test_cache_hits(expander: RecurrenceExpander) -> None:
    e = Event(
        uid="e3",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart=datetime(2026, 6, 1, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 1, tzinfo=timezone.utc),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    r1 = expander.expand_for_storage(e, start, end)
    r2 = expander.expand_for_storage(e, start, end)
    assert len(r1) == 2 and len(r2) == 2


def test_cache_hit_when_etag_changes_but_content_same(
    expander: RecurrenceExpander,
) -> None:
    """Bug 15: Cache should hit when only etag changes, not content."""
    e1 = Event(
        uid="e-etag",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart=datetime(2026, 7, 1, tzinfo=timezone.utc),
        dtend=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        etag='"v1"',
    )
    e2 = Event(
        uid="e-etag",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart=datetime(2026, 7, 1, tzinfo=timezone.utc),
        dtend=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        etag='"v2"',
    )
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, tzinfo=timezone.utc)
    r1 = expander.expand_for_storage(e1, start, end)
    r2 = expander.expand_for_storage(e2, start, end)
    assert len(r1) == 2
    assert len(r2) == 2
    assert r1 is r2


def test_cache_miss_when_dtstart_changes(expander: RecurrenceExpander) -> None:
    """Bug 15: Cache should miss when dtstart changes, even if uid is same."""
    e1 = Event(
        uid="e-dt",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart=datetime(2026, 8, 1, tzinfo=timezone.utc),
        dtend=datetime(2026, 8, 1, 1, tzinfo=timezone.utc),
    )
    e2 = Event(
        uid="e-dt",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart=datetime(2026, 8, 10, tzinfo=timezone.utc),
        dtend=datetime(2026, 8, 10, 1, tzinfo=timezone.utc),
    )
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 31, tzinfo=timezone.utc)
    r1 = expander.expand_for_storage(e1, start, end)
    r2 = expander.expand_for_storage(e2, start, end)
    assert len(r1) == 2
    assert len(r2) == 2
    assert r1 is not r2


# ── additional RRULE shapes ───────────────────────────────────────────────────


def test_weekly_byday_expands_mo_we_fr(expander: RecurrenceExpander) -> None:
    # MO/WE/FR for 2 weeks starting 2026-06-01 (Monday) → 6 occurrences in window
    e = Event(
        uid="weekly-byday",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=6",
        dtstart=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 6


def test_yearly_anniversary(expander: RecurrenceExpander) -> None:
    e = Event(
        uid="yearly-anniv",
        calendar_id="cal-1",
        rrule="FREQ=YEARLY;COUNT=2",
        dtstart=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
    )
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2028, 1, 1, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 2
    years = [r["dtstart"].year for r in results]
    assert years == [2026, 2027]


def test_until_terminator(expander: RecurrenceExpander) -> None:
    # UNTIL at May 15 → only May 13 and May 14 occurrences qualify
    e = Event(
        uid="until-test",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;UNTIL=20260515T000000Z",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 2


def test_exdate_removes_specific_occurrence(expander: RecurrenceExpander) -> None:
    exdate = datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc)
    e = Event(
        uid="exdate-test",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=5",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        exdates=(exdate,),
    )
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    # May 14 is excluded → 4 occurrences remain
    assert len(results) == 4
    dtstart_dates = [r["dtstart"] for r in results]
    assert exdate not in dtstart_dates


def test_rdate_adds_extra_occurrence(expander: RecurrenceExpander) -> None:
    rdate = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    e = Event(
        uid="rdate-test",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;COUNT=3",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        rdates=(rdate,),
    )
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    # RRULE gives May 13, 14, 15; RDATE adds May 20 → 4 occurrences
    assert len(results) == 4
    dtstart_dates = [r["dtstart"] for r in results]
    assert rdate in dtstart_dates


def test_indefinite_series_expands_within_pm_1y_window(
    expander: RecurrenceExpander,
) -> None:
    # No COUNT/UNTIL → expander limits to whatever the window covers
    e = Event(
        uid="indefinite",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    # 4-week window → 4 weekly occurrences
    start = datetime(2026, 5, 13, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 4
