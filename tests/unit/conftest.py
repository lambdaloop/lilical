"""Shared harness for QTest-driven UI regression tests.

Provides a session-scoped `QApplication`, a `FakeStore` factory mirroring the
`EventStore` signals/methods that views connect to, an `Event` factory anchored
to today, plan-shape builders for WeekView/DayView, and small QTest helpers
(`wait_until`, `mouse_move_to`) that paper over the offscreen platform's hover
quirks.

Tests should call `view._apply_plan(plan)` directly to bypass the async refresh
path and force-feed a deterministic layout.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timezone
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Ensure a usable asyncio event loop exists before every test.

    Some tests (google_writes_test.py) call asyncio.run() which clears the
    thread-local event loop (Python 3.12 behaviour).  Qt views call
    asyncio.ensure_future() inside showEvent / refresh(), which raises
    RuntimeError when no loop is set, leaving the coroutine unawaited and
    triggering a _warn_unawaited_coroutine crash during pytestqt teardown.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


def make_event(
    uid: str,
    *,
    hour: int = 9,
    minute: int = 0,
    duration_min: int = 60,
    cal_id: str = "cal-1",
    summary: str | None = None,
    location: str | None = None,
):
    """Construct a `lilical.models.event.Event` anchored to `date.today()`."""
    from lilical.models.event import Event

    today = date.today()
    start = datetime(
        today.year, today.month, today.day, hour, minute, tzinfo=timezone.utc
    )
    end_total = hour * 60 + minute + duration_min
    end_h, end_m = divmod(end_total, 60)
    if end_h >= 24:
        end_h = 23
        end_m = 59
    end = datetime(
        today.year, today.month, today.day, end_h, end_m, tzinfo=timezone.utc
    )
    return Event(
        uid=uid,
        calendar_id=cal_id,
        summary=summary or f"Event {uid}",
        location=location or "",
        dtstart=start,
        dtend=end,
    )


def make_fake_store():
    """Build a minimal `EventStore` stub: signals declared, query methods stubbed."""
    from PySide6.QtCore import QObject, Signal

    class FakeStore(QObject):
        events_changed = Signal(str, set)
        instances_changed = Signal(str, datetime, datetime)
        local_events_changed = Signal()
        cal_metadata_changed = Signal(str)
        instance_completion_changed = Signal(str, str, int)

        def list_instances(self, *_a, **_kw):
            return []

        def events_for_instances(self, _instances):
            return {}

        def completion_for_instances(self, _instances):
            return frozenset()

        def set_completed(self, *_a, **_kw):
            pass

        def queue_create(self, *_a, **_kw):
            pass

        def queue_update(self, *_a, **_kw):
            pass

        def set_account_orders(self, *_a, **_kw):
            pass

        def set_calendar_orders(self, *_a, **_kw):
            pass

    return FakeStore()


def wait_until(predicate, app, max_ms: int = 700, slice_ms: int = 50) -> bool:
    """Pump the Qt event loop in slices until predicate is true or max_ms elapses."""
    from PySide6.QtTest import QTest

    elapsed = 0
    while elapsed < max_ms:
        if predicate():
            return True
        QTest.qWait(slice_ms)
        app.processEvents()
        elapsed += slice_ms
    return predicate()


def mouse_move_to(view, scene_pos, app, *, baseline: bool = True) -> None:
    """Move the cursor to a scene point in `view`, with an out-of-rect baseline first.

    The baseline matters: when the widget is first shown the cursor may already
    be inside the target rect, so the subsequent move wouldn't synthesise a
    hoverEnter. Moving to topLeft first guarantees the target move is a real
    transition.
    """
    from PySide6.QtTest import QTest

    if baseline:
        QTest.mouseMove(view.viewport(), view.viewport().rect().topLeft())
        app.processEvents()
    vp_pt = view.mapFromScene(scene_pos)
    QTest.mouseMove(view.viewport(), vp_pt)
    app.processEvents()


def empty_week_plan() -> dict[str, Any]:
    """Base plan dict with every key WeekView._apply_plan expects."""
    return {
        "new_placements": {},
        "new_cluster_placements": {},
        "band_h": 30.0,
        "band_popover_events": {},
        "band_dense_cols": set(),
        "first_event_minutes": 540,
        "completions": frozenset(),
    }


def empty_day_plan() -> dict[str, Any]:
    """Base plan dict with every key _DayCanvas._apply_plan expects."""
    return {
        "new_placements": {},
        "new_cluster_placements": {},
        "band_h": 30.0,
        "first_event_minutes": 540,
        "completions": frozenset(),
    }


def placement_entry(
    event,
    rect,
    *,
    is_sticky: bool = False,
    inst_key=None,
    calendar_color: str | None = "#3498db",
    time_prefix: str | None = None,
    show_time_prefix: bool = True,
    instance_dtstart: datetime | None = None,
) -> dict[str, Any]:
    """Build one entry for plan['new_placements']."""
    return {
        "is_sticky": is_sticky,
        "inst_key": inst_key,
        "event": event,
        "rect": rect,
        "calendar_color": calendar_color,
        "time_prefix": time_prefix,
        "show_time_prefix": show_time_prefix,
        "instance_dtstart": instance_dtstart,
    }


def cluster_entry(
    rect,
    events_data: list[dict[str, Any]],
    *,
    dominant_index: int = 0,
    time_format: str = "24h",
    cal_color: str | None = "#3498db",
) -> dict[str, Any]:
    """Build one entry for plan['new_cluster_placements']."""
    cluster_start = min(e["start_min"] for e in events_data)
    cluster_end = max(e["end_min"] for e in events_data)
    return {
        "rect": rect,
        "cluster_data": {
            "events": events_data,
            "dominant_index": dominant_index,
            "cluster_start_min": float(cluster_start),
            "cluster_end_min": float(cluster_end),
        },
        "px_per_hour": 60.0,
        "calendar_color_map": {ev["cal_id"]: cal_color for ev in events_data},
        "time_format": time_format,
        "read_only_cal_ids": set(),
    }


def cluster_event_data(
    event,
    *,
    start_min: float,
    end_min: float,
    cal_id: str = "cal-1",
    cal_color: str | None = "#3498db",
) -> dict[str, Any]:
    """Build one entry for cluster_entry's events_data list."""
    return {
        "start_min": float(start_min),
        "end_min": float(end_min),
        "cal_id": cal_id,
        "payload": {
            "event": event,
            "key": f"key-{event.uid}",
            "show_time_prefix": True,
            "start_dt": event.dtstart,
            "instance_dtstart": None,
            "completed": False,
            "inst_key": None,
            "cal_color": cal_color,
        },
    }
