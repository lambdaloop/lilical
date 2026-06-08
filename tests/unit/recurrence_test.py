from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from lilical.models.event import Event
from lilical.recurrence.expander import RecurrenceExpander
from lilical.storage.event_store import EventStore


@pytest.fixture
def expander() -> RecurrenceExpander:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE events (
                uid TEXT NOT NULL,
                calendar_id TEXT NOT NULL,
                recurrence_id TEXT NOT NULL DEFAULT '',
                provider_event_id TEXT,
                dtstart TEXT NOT NULL,
                dtend TEXT NOT NULL,
                tz TEXT NOT NULL DEFAULT 'UTC',
                all_day INTEGER DEFAULT 0,
                summary TEXT DEFAULT '',
                description TEXT DEFAULT '',
                location TEXT DEFAULT '',
                url TEXT,
                rrule TEXT,
                exdates TEXT,
                rdates TEXT,
                attendees TEXT,
                organizer TEXT,
                categories TEXT,
                color TEXT,
                status TEXT DEFAULT 'CONFIRMED',
                self_response TEXT,
                transparency TEXT DEFAULT 'OPAQUE',
                valarms TEXT,
                etag TEXT,
                sequence INTEGER DEFAULT 0,
                last_modified TEXT,
                local_dirty INTEGER DEFAULT 0,
                deleted_locally INTEGER DEFAULT 0,
                conflict_state TEXT,
                local_modified_at TEXT,
                inserted_at TEXT,
                PRIMARY KEY(uid, calendar_id, recurrence_id)
            )
            """
        )
    store = EventStore(engine)
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


def test_until_terminator_non_utc_boundary(expander: RecurrenceExpander) -> None:
    """A UTC UNTIL must be honored against a non-UTC dtstart series (the form
    queue_split_series/queue_truncate_series now emit). 21:00 NZST daily with
    UNTIL=20260604T085959Z (== Jun 4 20:59:59 NZST) keeps Jun 2 and Jun 3 but
    drops the Jun 4 21:00 occurrence."""
    from zoneinfo import ZoneInfo

    nz = ZoneInfo("Pacific/Auckland")
    e = Event(
        uid="until-nz",
        calendar_id="cal-1",
        rrule="FREQ=DAILY;UNTIL=20260604T085959Z",
        dtstart=datetime(2026, 6, 2, 21, 0, tzinfo=nz),
        dtend=datetime(2026, 6, 2, 22, 0, tzinfo=nz),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    results = expander.expand_for_storage(e, start, end)
    assert len(results) == 2, [r["dtstart"] for r in results]


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


# ── override-aware expansion ──────────────────────────────────────────────────


def test_expander_suppresses_overridden_occurrence(
    expander: RecurrenceExpander,
) -> None:
    """An override at a specific recurrence_id replaces the rrule occurrence
    that would normally fall at that time."""
    master = Event(
        uid="override-suppress",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=4",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
    )
    week2_original = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    week2_moved = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    override = Event(
        uid="override-suppress",
        calendar_id="cal-1",
        recurrence_id=week2_original,
        dtstart=week2_moved,
        dtend=datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc),
    )
    window_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 30, tzinfo=timezone.utc)

    results = expander.expand_for_storage(
        master, window_start, window_end, overrides=[override]
    )

    # COUNT=4 → 4 total: 3 rrule occurrences + 1 override (week 2 replaced).
    assert len(results) == 4
    dtstart_values = [r["dtstart"] for r in results]
    assert week2_original not in dtstart_values  # original time suppressed
    assert week2_moved in dtstart_values  # override's modified time present
    override_rows = [r for r in results if r.get("is_override")]
    assert len(override_rows) == 1
    assert override_rows[0]["dtstart"] == week2_moved


def test_expander_suppresses_cancelled_override(
    expander: RecurrenceExpander,
) -> None:
    """A cancelled override (server single-occurrence deletion) must remove the
    slot entirely: the base rrule occurrence is suppressed and the cancelled
    override is not appended — a clean exdate-like hole."""
    master = Event(
        uid="override-cancel",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=4",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
    )
    week2 = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    cancelled = Event(
        uid="override-cancel",
        calendar_id="cal-1",
        recurrence_id=week2,
        dtstart=week2,
        dtend=datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
        status="CANCELLED",
    )
    window_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 30, tzinfo=timezone.utc)

    results = expander.expand_for_storage(
        master, window_start, window_end, overrides=[cancelled]
    )

    # COUNT=4 minus the cancelled week-2 slot → 3 occurrences, none at week2.
    assert len(results) == 3
    dtstart_values = [r["dtstart"] for r in results]
    assert week2 not in dtstart_values
    assert not any(r.get("is_override") for r in results)


def test_expander_keeps_confirmed_override(
    expander: RecurrenceExpander,
) -> None:
    """Guard against over-filtering: a CONFIRMED override is still rendered."""
    master = Event(
        uid="override-confirmed",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=4",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc),
    )
    week2 = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    moved = datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc)
    override = Event(
        uid="override-confirmed",
        calendar_id="cal-1",
        recurrence_id=week2,
        dtstart=moved,
        dtend=datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc),
        status="CONFIRMED",
    )
    window_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 30, tzinfo=timezone.utc)

    results = expander.expand_for_storage(
        master, window_start, window_end, overrides=[override]
    )

    assert len(results) == 4
    override_rows = [r for r in results if r.get("is_override")]
    assert len(override_rows) == 1
    assert override_rows[0]["dtstart"] == moved


def test_expander_excludes_overrides_outside_window(
    expander: RecurrenceExpander,
) -> None:
    """An override whose modified dtstart is outside the window is suppressed
    from rrule expansion but not appended — net effect: that occurrence vanishes."""
    master = Event(
        uid="override-window",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=2",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    # Override's recurrence_id matches week 2 (in-window), but its new dtstart
    # is in July (out of window).
    override = Event(
        uid="override-window",
        calendar_id="cal-1",
        recurrence_id=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        dtstart=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
    )
    window_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 1, tzinfo=timezone.utc)

    results = expander.expand_for_storage(
        master, window_start, window_end, overrides=[override]
    )

    # Week 2 original time is suppressed; July dtstart is outside the window.
    # Only week 1 (May 13) remains.
    assert len(results) == 1
    assert results[0]["dtstart"] == datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)


def test_dt_to_utc_with_naive_datetime() -> None:
    """_dt_to_utc handles naive datetime by assuming UTC."""
    from lilical.recurrence.expander import _dt_to_utc

    naive = datetime(2026, 5, 13, 9, 0)
    result = _dt_to_utc(naive)
    assert result.tzinfo is not None
    assert result == datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)


def test_expander_skips_override_without_recurrence_id(
    expander: RecurrenceExpander,
) -> None:
    """An override missing recurrence_id is skipped (continue)."""
    master = Event(
        uid="skip-override",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=2",
        dtstart=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    override_no_rid = Event(
        uid="skip-override",
        calendar_id="cal-1",
        recurrence_id=None,
        dtstart=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    results = expander.expand_for_storage(
        master, start, end, overrides=[override_no_rid]
    )
    assert len(results) == 2
    assert all(not r.get("is_override") for r in results)
    assert {r["dtstart"] for r in results} == {
        datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
    }


def test_expander_skips_override_without_dtstart(
    expander: RecurrenceExpander,
) -> None:
    """An override with recurrence_id but no dtstart is skipped (continue).
    The original rrule occurrence is still suppressed (by recurrence_id),
    but no replacement override is added."""
    master = Event(
        uid="skip-dtstart",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=2",
        dtstart=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    override_no_dtstart = Event(
        uid="skip-dtstart",
        calendar_id="cal-1",
        recurrence_id=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        dtstart=None,
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    results = expander.expand_for_storage(
        master, start, end, overrides=[override_no_dtstart]
    )
    assert len(results) == 1
    assert results[0]["dtstart"] == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def test_expander_cache_invalidates_on_override_change(
    expander: RecurrenceExpander,
) -> None:
    """Cache key includes the override hash: adding an override returns a different
    cached result even when event and window are identical."""
    master = Event(
        uid="override-cache",
        calendar_id="cal-1",
        rrule="FREQ=WEEKLY;COUNT=3",
        dtstart=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )
    window_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 6, 30, tzinfo=timezone.utc)

    no_overrides = expander.expand_for_storage(
        master, window_start, window_end, overrides=[]
    )

    override = Event(
        uid="override-cache",
        calendar_id="cal-1",
        recurrence_id=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        dtstart=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
    )
    with_override = expander.expand_for_storage(
        master, window_start, window_end, overrides=[override]
    )

    # Results must not be the same cached list object.
    assert no_overrides is not with_override
    # The dtstart sets must differ (override shifts week 2).
    no_ov_starts = {r["dtstart"] for r in no_overrides}
    ov_starts = {r["dtstart"] for r in with_override}
    assert no_ov_starts != ov_starts
