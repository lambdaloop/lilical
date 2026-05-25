"""End-to-end regression test for the dense-cluster hover popover chain.

Stands up a real `WeekView` against a stub `EventStore`, injects a
hand-crafted plan containing one dense cluster, drives a `QTest.mouseMove`
over it, and waits for the 280 ms timer to fire and the popover to appear.

Regression for the `.toPoint()` crash that was silently swallowed inside
the `QTimer.timeout` callback, preventing the popover from ever showing
even though the hover signal chain was working end-to-end.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


def _make_event(uid: str, minute_offset: int):
    from lilical.models.event import Event

    today = date.today()
    y, m, d = today.year, today.month, today.day
    start = datetime(y, m, d, 9, minute_offset, tzinfo=timezone.utc)
    end = datetime(y, m, d, 10, minute_offset, tzinfo=timezone.utc)
    return Event(
        uid=uid,
        calendar_id="cal-1",
        summary=f"Event {uid}",
        dtstart=start,
        dtend=end,
    )


def _build_fake_store():
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

    return FakeStore()


def _build_cluster_plan(rect_x: float = 200.0, rect_y: float = 540.0):
    """Hand-crafted plan with one dense 3-event cluster anchored at 09:00."""
    from PySide6.QtCore import QRectF

    events_data = []
    for i in range(3):
        s = 540 + i * 5  # 09:00, 09:05, 09:10 — dense overlap
        e = s + 60
        ev = _make_event(f"u{i}", i * 5)
        events_data.append(
            {
                "start_min": float(s),
                "end_min": float(e),
                "cal_id": "cal-1",
                "payload": {
                    "event": ev,
                    "key": f"key-{i}",
                    "show_time_prefix": True,
                    "start_dt": ev.dtstart,
                    "instance_dtstart": None,
                    "completed": False,
                    "inst_key": None,
                    "cal_color": "#3498db",
                },
            }
        )

    cluster_data = {
        "events": events_data,
        "dominant_index": 0,
        "cluster_start_min": 540.0,
        "cluster_end_min": 660.0,
    }
    cluster_rect = QRectF(rect_x, rect_y, 140.0, 120.0)
    cluster_key = ("cluster", 0, 540)
    return {
        "new_placements": {},
        "new_cluster_placements": {
            cluster_key: {
                "rect": cluster_rect,
                "cluster_data": cluster_data,
                "px_per_hour": 60.0,
                "calendar_color_map": {"cal-1": "#3498db"},
                "time_format": "24h",
                "read_only_cal_ids": set(),
            }
        },
        "band_h": 30.0,
        "band_popover_events": {},
        "band_dense_cols": set(),
        "first_event_minutes": 540,
        "completions": frozenset(),
    }


def _wait_until(predicate, app, max_ms: int = 700, slice_ms: int = 50) -> bool:
    from PySide6.QtTest import QTest

    elapsed = 0
    while elapsed < max_ms:
        if predicate():
            return True
        QTest.qWait(slice_ms)
        app.processEvents()
        elapsed += slice_ms
    return predicate()


def test_cluster_hover_shows_popover(qapp) -> None:
    """Hovering a dense cluster for >280 ms shows the side popover."""
    from PySide6.QtTest import QTest

    from lilical.ui.views.week import WeekView

    store = _build_fake_store()
    view = WeekView(store, day_count=7, cal_info_provider=lambda: {})
    try:
        view.resize(1200, 800)
        view.show()
        qapp.processEvents()

        view._apply_plan(_build_cluster_plan())
        qapp.processEvents()

        assert len(view._clusters) == 1, "expected exactly one cluster placement"
        cluster = next(iter(view._clusters.values()))
        vp_pt = view.mapFromScene(cluster.sceneBoundingRect().center())

        # Establish a baseline outside the cluster first so that the next
        # move triggers a hoverEnter rather than starting inside.
        QTest.mouseMove(view.viewport(), view.viewport().rect().topLeft())
        qapp.processEvents()
        QTest.mouseMove(view.viewport(), vp_pt)
        qapp.processEvents()

        appeared = _wait_until(
            lambda: view._cluster_popover.isVisible(), qapp, max_ms=700
        )
        assert appeared, "cluster popover never became visible after hover"

        # Sanity: geometry is non-degenerate.
        geom = view._cluster_popover.geometry()
        assert geom.width() > 0 and geom.height() > 0, (
            f"degenerate popover geometry: {geom}"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()
