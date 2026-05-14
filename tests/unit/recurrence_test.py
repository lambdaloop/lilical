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
