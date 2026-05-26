"""Tests for WeekView's all-day band hover popover.

The hover popover is timer-driven (`_band_show_timer`, 280 ms) and only
fires on columns the plan marked as dense (`band_dense_cols`).  Tests:

- A dense column shows the popover after the timer fires.
- A non-dense column does *not* show the popover even after a long wait.
- Leaving the band hides the popover.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest

from tests.unit.conftest import (
    empty_week_plan,
    make_fake_store,
    wait_until,
)


def _build_view(qapp):
    from lilical.ui.views.week import WeekView

    store = make_fake_store()
    view = WeekView(store, day_count=7, cal_info_provider=lambda: {})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    return view


def _plan_with_band(*, dense_cols: set[int], col_events: dict[int, list]) -> dict:
    """Build a plan with the band sized for a real hover target."""
    from lilical.ui.widgets._popover_rows import PopoverEvent

    plan = empty_week_plan()
    plan["band_h"] = 60.0  # tall enough to give a generous hover target
    plan["band_dense_cols"] = dense_cols
    plan["band_popover_events"] = {
        col: [
            PopoverEvent(
                time_str="All day",
                title=f"Event {i}",
                location=None,
                calendar_color="#3498db",
                uid=f"u{col}-{i}",
            )
            for i, _ in enumerate(events)
        ]
        for col, events in col_events.items()
    }
    return plan


def _band_point_for_col(view, col: int) -> QPoint:
    """Compute a viewport point inside column `col`'s all-day band."""
    from lilical.ui.views.week import DAY_HEADER_H, TIME_AXIS_WIDTH

    body_w = view.viewport().width() - TIME_AXIS_WIDTH
    col_w = body_w / view._day_count
    x = int(TIME_AXIS_WIDTH + (col + 0.5) * col_w)
    y = int(DAY_HEADER_H + view._current_band_h / 2)
    return QPoint(x, y)


def test_dense_band_column_hover_shows_popover(qapp) -> None:
    """Hovering a dense column triggers the band popover after the timer."""
    view = _build_view(qapp)
    try:
        plan = _plan_with_band(
            dense_cols={2},
            col_events={2: [object(), object(), object()]},
        )
        view._apply_plan(plan)
        qapp.processEvents()

        # Baseline move outside the band so the next move is a real transition.
        QTest.mouseMove(view.viewport(), QPoint(1, 1))
        qapp.processEvents()
        QTest.mouseMove(view.viewport(), _band_point_for_col(view, 2))
        qapp.processEvents()

        shown = wait_until(
            lambda: view._popover.isVisible(), qapp, max_ms=700
        )
        assert shown, "band popover never appeared over a dense column"
    finally:
        view._popover.hide()
        view._band_show_timer.stop()
        view.close()
        view.deleteLater()


def test_non_dense_band_column_does_not_show_popover(qapp) -> None:
    """A column not in band_dense_cols should not start the show timer."""
    view = _build_view(qapp)
    try:
        plan = _plan_with_band(dense_cols=set(), col_events={})
        view._apply_plan(plan)
        qapp.processEvents()

        QTest.mouseMove(view.viewport(), QPoint(1, 1))
        qapp.processEvents()
        QTest.mouseMove(view.viewport(), _band_point_for_col(view, 2))
        qapp.processEvents()

        assert not view._band_show_timer.isActive(), (
            "non-dense column should not start the band show timer"
        )

        QTest.qWait(400)
        qapp.processEvents()
        assert not view._popover.isVisible(), (
            "popover appeared over a non-dense column"
        )
    finally:
        view._popover.hide()
        view._band_show_timer.stop()
        view.close()
        view.deleteLater()


def test_leaving_band_hides_popover(qapp) -> None:
    """Once shown, leaving the column hides the popover."""
    view = _build_view(qapp)
    try:
        plan = _plan_with_band(
            dense_cols={1},
            col_events={1: [object(), object()]},
        )
        view._apply_plan(plan)
        qapp.processEvents()

        QTest.mouseMove(view.viewport(), QPoint(1, 1))
        qapp.processEvents()
        QTest.mouseMove(view.viewport(), _band_point_for_col(view, 1))
        qapp.processEvents()
        assert wait_until(
            lambda: view._popover.isVisible(), qapp, max_ms=700
        ), "popover never appeared"

        # Move to a different column → _update_band_hover hides the popover.
        QTest.mouseMove(view.viewport(), _band_point_for_col(view, 4))
        qapp.processEvents()
        assert not view._popover.isVisible(), (
            "popover did not hide when cursor moved to a different column"
        )
    finally:
        view._popover.hide()
        view._band_show_timer.stop()
        view.close()
        view.deleteLater()
