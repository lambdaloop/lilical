"""Headless smoke tests for EventChip drag interactions.

Constructs an EventChip in an offscreen QGraphicsScene, sends synthetic
QGraphicsSceneMouseEvent objects, and asserts the drag_progress /
drag_committed / drag_cancelled / edit_requested signals fire with the
correct payloads.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_event(uid: str = "e1"):
    from lilical.models.event import Event

    return Event(uid=uid, calendar_id="cal-1", summary="Test event")


def _make_chip(event=None, rect=None):
    from PySide6.QtCore import QRectF

    from lilical.ui.widgets.event_chip import ChipMode, EventChip

    if event is None:
        event = _make_event()
    if rect is None:
        rect = QRectF(10, 100, 120, 60)
    return EventChip(event, rect, mode=ChipMode.BARS, show_time_prefix=True)


def _press(chip, pos, scene_pos=None):
    """Send a left-button press to chip at local `pos`."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent
    from PySide6.QtCore import Qt

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMousePress)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.LeftButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mousePressEvent(ev)


def _move(chip, pos, scene_pos=None):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent
    from PySide6.QtCore import Qt

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseMove)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.LeftButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mouseMoveEvent(ev)


def _release(chip, pos, scene_pos=None):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent
    from PySide6.QtCore import Qt

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseRelease)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.NoButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mouseReleaseEvent(ev)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_body_drag_emits_progress_and_committed(qapp):
    """Press in body, move > MOVE_THRESHOLD_PX → drag_progress + drag_committed."""
    from PySide6.QtCore import QPointF

    chip = _make_chip()
    progress_calls: list[tuple] = []
    committed_calls: list[tuple] = []

    chip.drag_progress.connect(lambda e, m, p: progress_calls.append((e, m, p)))
    chip.drag_committed.connect(lambda e, m, p: committed_calls.append((e, m, p)))

    center = chip.boundingRect().center()
    # Press in the middle of the chip
    _press(chip, center)

    # Move 1 px — below threshold, should NOT emit yet
    _move(chip, center + QPointF(1, 0))
    assert not progress_calls

    # Move 10 px right — crosses MOVE_THRESHOLD_PX (=4)
    _move(chip, center + QPointF(10, 0))
    assert len(progress_calls) == 1
    ev, mode, _ = progress_calls[0]
    assert mode == "move"

    # More moves keep emitting
    _move(chip, center + QPointF(20, 5))
    assert len(progress_calls) == 2

    # Release → committed
    _release(chip, center + QPointF(20, 5))
    assert len(committed_calls) == 1
    ev2, mode2, _ = committed_calls[0]
    assert mode2 == "move"
    assert ev2 is chip._event


def test_click_without_drag_emits_edit_requested(qapp):
    """Press + release without exceeding threshold → edit_requested (no drag signals)."""
    from PySide6.QtCore import QPointF

    chip = _make_chip()
    progress_calls: list = []
    committed_calls: list = []
    edit_calls: list = []

    chip.drag_progress.connect(lambda *a: progress_calls.append(a))
    chip.drag_committed.connect(lambda *a: committed_calls.append(a))
    chip.edit_requested.connect(lambda e: edit_calls.append(e))

    center = chip.boundingRect().center()
    _press(chip, center)
    # Tiny wobble — stays below threshold
    _move(chip, center + QPointF(1, 1))
    _release(chip, center + QPointF(1, 1))

    assert not progress_calls
    assert not committed_calls
    assert len(edit_calls) == 1
    assert edit_calls[0] is chip._event


def test_resize_top_edge(qapp):
    """Press near top edge → mode is resize_top on first move past threshold."""
    from PySide6.QtCore import QPointF

    chip = _make_chip()
    progress_calls: list = []
    chip.drag_progress.connect(lambda e, m, p: progress_calls.append((e, m, p)))

    # Press 3 px from the top edge (within EDGE_RESIZE_PX = 6)
    rect = chip.boundingRect()
    top_pos = QPointF(rect.center().x(), rect.top() + 3)
    _press(chip, top_pos)

    # Chip should immediately be in resize_top (no pending promotion needed)
    assert chip._drag_mode == "resize_top"

    # Move down 10 px → should emit drag_progress with mode resize_top
    _move(chip, top_pos + QPointF(0, 10))
    assert len(progress_calls) == 1
    _, mode, _ = progress_calls[0]
    assert mode == "resize_top"


def test_resize_bottom_edge(qapp):
    """Press near bottom edge → mode is resize_bottom."""
    from PySide6.QtCore import QPointF

    chip = _make_chip()
    progress_calls: list = []
    chip.drag_progress.connect(lambda e, m, p: progress_calls.append((e, m, p)))

    rect = chip.boundingRect()
    bottom_pos = QPointF(rect.center().x(), rect.bottom() - 3)
    _press(chip, bottom_pos)
    assert chip._drag_mode == "resize_bottom"

    _move(chip, bottom_pos + QPointF(0, 10))
    assert len(progress_calls) == 1
    _, mode, _ = progress_calls[0]
    assert mode == "resize_bottom"


def test_cancel_drag_emits_drag_cancelled(qapp):
    """cancel_drag() during an active drag emits drag_cancelled."""
    from PySide6.QtCore import QPointF

    chip = _make_chip()
    cancelled_calls: list = []
    chip.drag_cancelled.connect(lambda e: cancelled_calls.append(e))

    center = chip.boundingRect().center()
    _press(chip, center)
    _move(chip, center + QPointF(10, 0))  # promote to move

    chip.cancel_drag()
    assert len(cancelled_calls) == 1
    assert cancelled_calls[0] is chip._event
    assert chip._drag_mode is None


def test_small_chip_no_edge_resize(qapp):
    """Chips shorter than MIN_HEIGHT_FOR_EDGE_RESIZE (18 px) never enter edge-resize."""
    from PySide6.QtCore import QPointF, QRectF

    small_chip = _make_chip(rect=QRectF(10, 100, 120, 14))
    assert not small_chip._can_edge_resize()

    progress_calls: list = []
    small_chip.drag_progress.connect(lambda e, m, p: progress_calls.append((e, m, p)))

    # Press near the very top — should still be "pending" not "resize_top"
    top_pos = QPointF(small_chip.boundingRect().center().x(),
                      small_chip.boundingRect().top() + 2)
    _press(small_chip, top_pos)
    assert small_chip._drag_mode == "pending"

    # Drag crosses threshold → promoted to "move", not resize
    _move(small_chip, top_pos + QPointF(10, 0))
    assert len(progress_calls) == 1
    _, mode, _ = progress_calls[0]
    assert mode == "move"
