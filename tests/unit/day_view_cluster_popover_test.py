"""Regression test for the DayView cluster hover popover.

DayView mirrors WeekView's cluster-popover pipeline; both have the same
`.toPoint()` bug shape (see commit 37f475d).  A single hover test confirms
the mirror stays in sync — if the bug is reintroduced in `day.py`
`_show_cluster_popover`, this test fails the same way the WeekView one does.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QRectF

from tests.unit.conftest import (
    cluster_entry,
    cluster_event_data,
    empty_day_plan,
    make_event,
    make_fake_store,
    mouse_move_to,
    wait_until,
)


def _build_cluster_plan():
    events_data = []
    for i in range(3):
        s = 540 + i * 5  # 09:00, 09:05, 09:10 — dense overlap
        e = s + 60
        ev = make_event(f"u{i}", hour=9, minute=i * 5)
        events_data.append(cluster_event_data(ev, start_min=s, end_min=e))
    rect = QRectF(200.0, 540.0, 140.0, 120.0)
    plan = empty_day_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(rect, events_data)
    }
    return plan


def test_day_view_cluster_hover_shows_popover(qapp) -> None:
    """Hovering a dense cluster in DayView shows the side popover."""
    from lilical.ui.views.day import _DayCanvas

    store = make_fake_store()
    canvas = _DayCanvas(store, date.today(), cal_info_provider=lambda: {})
    canvas.resize(800, 800)
    canvas.show()
    qapp.processEvents()
    canvas._apply_plan(_build_cluster_plan())
    qapp.processEvents()

    try:
        assert len(canvas._clusters) == 1, "expected exactly one cluster placement"
        cluster = next(iter(canvas._clusters.values()))
        mouse_move_to(canvas, cluster.sceneBoundingRect().center(), qapp)

        appeared = wait_until(
            lambda: canvas._cluster_popover.isVisible(), qapp, max_ms=700
        )
        assert appeared, "DayView cluster popover never became visible after hover"

        geom = canvas._cluster_popover.geometry()
        assert geom.width() > 0 and geom.height() > 0, (
            f"degenerate popover geometry: {geom}"
        )
    finally:
        canvas._cluster_popover.hide()
        canvas.close()
        canvas.deleteLater()


def test_day_view_spine_click_shows_popover_immediately(qapp) -> None:
    """Spine-click on DayView cluster shows popover synchronously."""
    from lilical.ui.views.day import _DayCanvas

    store = make_fake_store()
    canvas = _DayCanvas(store, date.today(), cal_info_provider=lambda: {})
    canvas.resize(800, 800)
    canvas.show()
    qapp.processEvents()
    canvas._apply_plan(_build_cluster_plan())
    qapp.processEvents()

    try:
        cluster = next(iter(canvas._clusters.values()))
        cluster.spine_clicked.emit(cluster.cluster_events)
        qapp.processEvents()
        assert canvas._cluster_popover.isVisible(), (
            "spine click did not show popover synchronously"
        )
        assert not canvas._cluster_show_timer.isActive(), (
            "spine click should not start the hover timer"
        )
    finally:
        canvas._cluster_popover.hide()
        canvas.close()
        canvas.deleteLater()
