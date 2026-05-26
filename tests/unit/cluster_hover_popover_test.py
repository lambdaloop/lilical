"""End-to-end regression tests for the dense-cluster hover popover chain.

Stands up a real `WeekView` against a stub `EventStore`, force-feeds a plan
with one hand-crafted dense cluster, and drives the hover / spine-click /
re-entry interactions via QTest.

Regression for the `.toPoint()` crash that was silently swallowed inside
the `QTimer.timeout` callback (commit 37f475d), plus surrounding coverage
for the hide-on-leave grace window, popover re-entry tolerance, and
synchronous spine-click activation.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF

from lilical.ui.views.week import TIME_AXIS_WIDTH, WeekView
from tests.unit.conftest import (
    cluster_entry,
    cluster_event_data,
    empty_week_plan,
    make_event,
    make_fake_store,
    mouse_move_to,
    wait_until,
)


def _build_column_cluster_plan(day_index: int, col_w: int) -> dict:
    """Plan with one cluster whose left edge is exactly at a column boundary."""
    events_data = []
    for i in range(3):
        s = 540 + i * 5
        e = s + 60
        ev = make_event(f"cp{i}", hour=9, minute=i * 5)
        events_data.append(cluster_event_data(ev, start_min=s, end_min=e))
    rect_x = TIME_AXIS_WIDTH + day_index * col_w
    rect = QRectF(rect_x, 540.0, col_w, 120.0)
    plan = empty_week_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(rect, events_data)
    }
    return plan


def _make_week_view(qapp, *, day_count: int = 7, width: int = 1200):
    store = make_fake_store()
    view = WeekView(store, day_count=day_count, cal_info_provider=lambda: {})
    view.resize(width, 800)
    view.show()
    qapp.processEvents()
    return view


def _build_cluster_plan(rect_x: float = 200.0, rect_y: float = 540.0):
    """Hand-crafted plan with one dense 3-event cluster anchored at 09:00."""
    events_data = []
    for i in range(3):
        s = 540 + i * 5  # 09:00, 09:05, 09:10 — dense overlap
        e = s + 60
        ev = make_event(f"u{i}", hour=9, minute=i * 5)
        events_data.append(
            cluster_event_data(ev, start_min=s, end_min=e)
        )
    rect = QRectF(rect_x, rect_y, 140.0, 120.0)
    plan = empty_week_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(rect, events_data)
    }
    return plan


def _build_view_with_cluster(qapp):
    """Construct a WeekView, show it, and apply the cluster plan."""
    from lilical.ui.views.week import WeekView

    store = make_fake_store()
    view = WeekView(store, day_count=7, cal_info_provider=lambda: {})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    view._apply_plan(_build_cluster_plan())
    qapp.processEvents()
    return view


def test_cluster_hover_shows_popover(qapp) -> None:
    """Hovering a dense cluster for >280 ms shows the side popover."""
    view = _build_view_with_cluster(qapp)
    try:
        assert len(view._clusters) == 1, "expected exactly one cluster placement"
        cluster = next(iter(view._clusters.values()))
        mouse_move_to(view, cluster.sceneBoundingRect().center(), qapp)

        appeared = wait_until(
            lambda: view._cluster_popover.isVisible(), qapp, max_ms=700
        )
        assert appeared, "cluster popover never became visible after hover"

        geom = view._cluster_popover.geometry()
        assert geom.width() > 0 and geom.height() > 0, (
            f"degenerate popover geometry: {geom}"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()


def test_cluster_popover_hides_when_cursor_leaves(qapp) -> None:
    """Leaving the cluster hides the popover after the grace period."""
    view = _build_view_with_cluster(qapp)
    try:
        cluster = next(iter(view._clusters.values()))
        mouse_move_to(view, cluster.sceneBoundingRect().center(), qapp)
        assert wait_until(
            lambda: view._cluster_popover.isVisible(), qapp, max_ms=700
        ), "popover never appeared"

        # Trigger hover_left → schedule_hide. Sending mouseMove past the
        # cluster's right edge invokes LineCluster.hoverLeaveEvent.
        from PySide6.QtTest import QTest

        cluster_right = cluster.sceneBoundingRect().right()
        far_right = view.mapFromScene(
            cluster.sceneBoundingRect().center()
        )
        QTest.mouseMove(
            view.viewport(),
            QPoint(int(far_right.x() + (cluster_right + 200)), far_right.y()),
        )
        qapp.processEvents()
        view._on_cluster_hover_left()  # belt + suspenders for the offscreen platform

        # 150 ms hide grace + slack.
        hidden = wait_until(
            lambda: not view._cluster_popover.isVisible(), qapp, max_ms=500
        )
        assert hidden, "popover did not hide after cursor left"
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()


def test_re_entering_popover_cancels_hide(qapp) -> None:
    """Entering the popover after a hide is scheduled cancels the timer."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QEnterEvent

    view = _build_view_with_cluster(qapp)
    try:
        cluster = next(iter(view._clusters.values()))
        mouse_move_to(view, cluster.sceneBoundingRect().center(), qapp)
        assert wait_until(
            lambda: view._cluster_popover.isVisible(), qapp, max_ms=700
        )

        # Start the hide timer, then send the popover a synthetic enterEvent
        # to cancel it — mirrors the user moving from the cluster into the
        # popover within the 150 ms grace.
        view._on_cluster_hover_left()
        assert view._cluster_popover._hide_timer.isActive()
        enter = QEnterEvent(QPointF(5.0, 5.0), QPointF(5.0, 5.0), QPointF(5.0, 5.0))
        view._cluster_popover.enterEvent(enter)
        assert not view._cluster_popover._hide_timer.isActive(), (
            "enterEvent should cancel the hide timer"
        )

        # After the grace period passes, the popover should still be visible.
        from PySide6.QtTest import QTest

        QTest.qWait(250)
        qapp.processEvents()
        assert view._cluster_popover.isVisible(), (
            "popover hid despite re-entry cancelling the timer"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()


def test_spine_click_shows_popover_immediately(qapp) -> None:
    """Spine-clicked signal shows the popover without waiting for the 280 ms timer."""
    view = _build_view_with_cluster(qapp)
    try:
        cluster = next(iter(view._clusters.values()))
        # Drive the signal directly — _on_cluster_spine_clicked calls
        # _show_cluster_popover synchronously, bypassing the show timer.
        cluster.spine_clicked.emit(cluster.cluster_events)
        qapp.processEvents()
        assert view._cluster_popover.isVisible(), (
            "spine click did not show popover synchronously"
        )
        assert not view._cluster_show_timer.isActive(), (
            "spine click should not start the hover timer"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()


def test_cluster_popover_x_aligns_right_of_column(qapp) -> None:
    """Popover left edge must sit flush at the cluster column's right edge."""
    view = _make_week_view(qapp)
    col_w = max(1, (view.viewport().width() - TIME_AXIS_WIDTH) // 7)
    day_index = 2
    view._apply_plan(_build_column_cluster_plan(day_index, col_w))
    qapp.processEvents()
    try:
        cluster = next(iter(view._clusters.values()))
        cluster.spine_clicked.emit(cluster.cluster_events)
        qapp.processEvents()

        pop = view._cluster_popover
        assert pop.isVisible()

        expected_x = view.viewport().mapToGlobal(
            QPoint(TIME_AXIS_WIDTH + (day_index + 1) * col_w, 0)
        ).x()
        assert abs(pop.geometry().x() - expected_x) <= 2, (
            f"popover x={pop.geometry().x()} expected ≈{expected_x} "
            f"(col_right of column {day_index})"
        )
        assert abs(pop.width() - col_w) <= 2, (
            f"popover width={pop.width()} expected ≈{col_w}"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()


def test_cluster_popover_flips_left_on_last_column(qapp) -> None:
    """Popover flips left when the rightmost column has no space to the right.

    Uses a narrow viewport (500 px) so the flipped popover stays within the
    offscreen platform's virtual screen (800 px wide) and avail-clamping
    doesn't interfere with the position assertion.
    """
    view = _make_week_view(qapp, width=500)
    col_w = max(1, (view.viewport().width() - TIME_AXIS_WIDTH) // 7)
    day_index = 6  # last column
    view._apply_plan(_build_column_cluster_plan(day_index, col_w))
    qapp.processEvents()
    try:
        cluster = next(iter(view._clusters.values()))
        cluster.spine_clicked.emit(cluster.cluster_events)
        qapp.processEvents()

        pop = view._cluster_popover
        assert pop.isVisible()

        # Flipped left: popover's right edge ≈ column's left edge.
        expected_col_left = view.viewport().mapToGlobal(
            QPoint(TIME_AXIS_WIDTH + day_index * col_w, 0)
        ).x()
        assert abs((pop.geometry().x() + pop.width()) - expected_col_left) <= 2, (
            f"popover right edge={pop.geometry().x() + pop.width()} "
            f"expected ≈{expected_col_left} (col_left of column {day_index})"
        )
    finally:
        view._cluster_popover.hide()
        view.close()
        view.deleteLater()
