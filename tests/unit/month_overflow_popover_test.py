"""Tests for MonthView's `+N more` overflow indicator.

The `_OverflowChip` was the site of the `968a321` crash and `02062d5`
revert.  Clicking the chip emits `day_activated(date)` for view-switching.
These tests force-feed a month plan with one day intentionally overfilled,
assert the chip renders, click it, and assert the signal fires with that day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QGraphicsSceneMouseEvent

from tests.unit.conftest import make_event, make_fake_store


@dataclass
class FakeInst:
    uid: str
    calendar_id: str
    dtstart_local: str
    dtend_local: str
    dtstart_utc: int
    all_day: int = 0
    recurrence_id: str = ""


def _build_view(qapp):
    from lilical.ui.views.month import MonthView

    store = make_fake_store()
    view = MonthView(store, cal_info_provider=lambda: {})
    view.resize(1100, 700)
    view.show()
    qapp.processEvents()
    return view


def _overflow_plan_for(view, target_day: date, n_events: int) -> dict[str, Any]:
    """Plan with `n_events` 1-hour events on `target_day` to force overflow.

    With CELL_H=100, CHIP_H=16, CHIP_GAP=2 → max_chips_per_cell = 4.  Passing
    n_events > 4 reliably trips the +N more affordance.
    """
    grid_start = view._grid.grid_start
    instances: list[FakeInst] = []
    events: dict[int, Any] = {}
    for i in range(n_events):
        hh = 9 + i  # 09:00, 10:00, …
        start_iso = datetime(
            target_day.year, target_day.month, target_day.day, hh, 0
        ).isoformat()
        end_iso = datetime(
            target_day.year, target_day.month, target_day.day, hh + 1, 0
        ).isoformat()
        inst = FakeInst(
            uid=f"u{i}",
            calendar_id="cal-1",
            dtstart_local=start_iso,
            dtend_local=end_iso,
            dtstart_utc=i,
        )
        instances.append(inst)
        events[id(inst)] = make_event(
            f"u{i}", hour=hh, minute=0, summary=f"Event {i}"
        )
    return {
        "instances": instances,
        "events": events,
        "cal_color": {"cal-1": "#3498db"},
        "grid_start": grid_start,
        "completions": frozenset(),
    }


def _find_overflow_chip(view):
    from lilical.ui.views.month import _OverflowChip

    chips = [c for c in view._chips if isinstance(c, _OverflowChip)]
    return chips


def test_overflow_chip_renders_when_cell_overflows(qapp) -> None:
    """A day with more events than max_chips_per_cell gets a +N marker."""
    view = _build_view(qapp)
    try:
        # Use a day mid-grid so it can't fall outside the visible range.
        target_day = view._grid.grid_start + timedelta(days=10)
        view._apply_plan(_overflow_plan_for(view, target_day, n_events=8))
        qapp.processEvents()

        chips = _find_overflow_chip(view)
        assert len(chips) == 1, (
            f"expected exactly one overflow chip, got {len(chips)}"
        )
        assert chips[0]._for_day == target_day, (
            "overflow chip points at the wrong day"
        )
        assert chips[0]._label.startswith("+") and "more" in chips[0]._label, (
            f"unexpected overflow label: {chips[0]._label!r}"
        )
    finally:
        view.close()
        view.deleteLater()


def test_no_overflow_chip_when_cell_within_budget(qapp) -> None:
    """A day with fewer events than the cell budget has no overflow chip."""
    view = _build_view(qapp)
    try:
        target_day = view._grid.grid_start + timedelta(days=10)
        view._apply_plan(_overflow_plan_for(view, target_day, n_events=2))
        qapp.processEvents()

        assert _find_overflow_chip(view) == [], (
            "overflow chip rendered for an under-budget cell"
        )
    finally:
        view.close()
        view.deleteLater()


def test_clicking_overflow_chip_emits_day_activated(qapp) -> None:
    """Left-clicking the chip emits MonthView.day_activated with the cell day."""
    view = _build_view(qapp)
    try:
        target_day = view._grid.grid_start + timedelta(days=10)
        view._apply_plan(_overflow_plan_for(view, target_day, n_events=8))
        qapp.processEvents()

        chip = _find_overflow_chip(view)[0]

        received: list[date] = []
        view.day_activated.connect(received.append)

        # Synthesise a QGraphicsSceneMouseEvent for the chip directly — driving
        # the scene through QTest.mouseClick is brittle for nested items.
        ev = QGraphicsSceneMouseEvent(QMouseEvent.Type.GraphicsSceneMousePress)
        ev.setButton(Qt.MouseButton.LeftButton)
        ev.setButtons(Qt.MouseButton.LeftButton)
        chip.mousePressEvent(ev)
        qapp.processEvents()

        assert received == [target_day], (
            f"day_activated did not fire with {target_day}, got {received}"
        )
    finally:
        view.close()
        view.deleteLater()
