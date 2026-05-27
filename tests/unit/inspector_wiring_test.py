"""Integration tests: chip / cluster hover → InspectorPane updates.

The pane itself is unit-tested in `inspector_pane_test.py`; this file
covers the wiring from `WeekView` / `_DayCanvas` into it so refactors of
the hover plumbing don't silently break the live UI flow.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QRectF

from lilical.ui.views.day import _DayCanvas
from lilical.ui.views.week import TIME_AXIS_WIDTH, WeekView
from lilical.ui.widgets.inspector_pane import InspectorPane
from tests.unit.conftest import (
    cluster_entry,
    cluster_event_data,
    empty_day_plan,
    empty_week_plan,
    make_event,
    make_fake_store,
    placement_entry,
)

# ─── WeekView ─────────────────────────────────────────────────────────


def _build_week_with_single_chip(qapp, inspector):
    store = make_fake_store()
    view = WeekView(
        store, day_count=7, cal_info_provider=lambda: {}, inspector=inspector
    )
    view.resize(1200, 800)
    view.show()
    inspector.show()  # must be visible so _on_event_hovered routes to it
    qapp.processEvents()

    ev = make_event("solo", hour=10, minute=0, summary="Lone meeting")
    plan = empty_week_plan()
    plan["new_placements"] = {
        ("solo", 0): placement_entry(ev, QRectF(200, 600, 120, 60))
    }
    view._apply_plan(plan)
    qapp.processEvents()
    return view


def _build_week_with_cluster(qapp, inspector):
    store = make_fake_store()
    view = WeekView(
        store, day_count=7, cal_info_provider=lambda: {}, inspector=inspector
    )
    view.resize(1200, 800)
    view.show()
    inspector.show()  # must be visible so hover signals route to it
    qapp.processEvents()

    events_data = []
    for i in range(3):
        s = 540 + i * 5
        e = s + 60
        events_data.append(
            cluster_event_data(
                make_event(f"c{i}", hour=9, minute=i * 5),
                start_min=s,
                end_min=e,
            )
        )
    plan = empty_week_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(
            QRectF(TIME_AXIS_WIDTH, 540, 140, 120), events_data
        )
    }
    view._apply_plan(plan)
    qapp.processEvents()
    return view


def test_week_chip_hover_populates_inspector(qapp) -> None:
    inspector = InspectorPane()
    view = _build_week_with_single_chip(qapp, inspector)
    try:
        assert len(view._chips) == 1
        chip = next(iter(view._chips.values()))
        # Drive the chip's hovered signal — the view's _wire_chip_signals
        # connects it to _on_event_hovered which forwards to the inspector.
        chip.hovered.emit(chip._to_popover_event(), "some notes")
        qapp.processEvents()
        assert inspector._title.text() == "Lone meeting"
        assert "some notes" in inspector._notes.text()
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_week_chip_hover_left_clears_inspector(qapp) -> None:
    inspector = InspectorPane()
    view = _build_week_with_single_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        chip.hovered.emit(chip._to_popover_event(), None)
        qapp.processEvents()
        assert inspector._title.text() == "Lone meeting"
        chip.hover_left.emit()
        qapp.processEvents()
        assert inspector._title.text() == ""
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_week_cluster_hover_populates_inspector_cluster_section(qapp) -> None:
    inspector = InspectorPane()
    view = _build_week_with_cluster(qapp, inspector)
    try:
        assert len(view._clusters) == 1
        cluster = next(iter(view._clusters.values()))
        cluster.hovered.emit(cluster.cluster_events)
        qapp.processEvents()
        # Dominant index defaults to 0 → first event, summary "Event c0".
        assert inspector._title.text() == "Event c0"
        assert "3 EVENTS" in inspector._cluster_header.text()
        assert len(inspector._current_rows) == 3
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_week_cluster_hover_left_clears_inspector(qapp) -> None:
    inspector = InspectorPane()
    view = _build_week_with_cluster(qapp, inspector)
    try:
        cluster = next(iter(view._clusters.values()))
        cluster.hovered.emit(cluster.cluster_events)
        qapp.processEvents()
        assert inspector._cluster_header.text() != ""
        cluster.hover_left.emit()
        qapp.processEvents()
        assert inspector._title.text() == ""
        assert inspector._current_rows == []
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


# ─── DayView (_DayCanvas) ─────────────────────────────────────────────


def _build_day_canvas_with_cluster(qapp, inspector):
    store = make_fake_store()
    canvas = _DayCanvas(
        store, date.today(), cal_info_provider=lambda: {}, inspector=inspector
    )
    canvas.resize(800, 800)
    canvas.show()
    inspector.show()  # must be visible so hover signals route to it
    qapp.processEvents()

    events_data = []
    for i in range(3):
        s = 540 + i * 5
        e = s + 60
        events_data.append(
            cluster_event_data(
                make_event(f"d{i}", hour=9, minute=i * 5),
                start_min=s,
                end_min=e,
            )
        )
    plan = empty_day_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(
            QRectF(200, 540, 140, 120), events_data
        )
    }
    canvas._apply_plan(plan)
    qapp.processEvents()
    return canvas


def test_day_cluster_hover_populates_inspector(qapp) -> None:
    inspector = InspectorPane()
    canvas = _build_day_canvas_with_cluster(qapp, inspector)
    try:
        cluster = next(iter(canvas._clusters.values()))
        cluster.hovered.emit(cluster.cluster_events)
        qapp.processEvents()
        assert inspector._title.text() == "Event d0"
        assert len(inspector._current_rows) == 3
    finally:
        canvas.close()
        canvas.deleteLater()
        inspector.deleteLater()
