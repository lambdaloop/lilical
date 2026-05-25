"""Tests for the read-only behavior of EventChip (subscription / reader-tier
calendars). Mirrors the offscreen-Qt pattern in chip_drag_test.py."""

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


def _make_chip(*, read_only: bool, rect=None):
    from PySide6.QtCore import QRectF

    from lilical.ui.widgets.event_chip import ChipMode, EventChip

    return EventChip(
        _make_event(),
        rect or QRectF(10, 100, 120, 60),
        mode=ChipMode.BARS,
        show_time_prefix=True,
        read_only=read_only,
    )


def _press(chip, pos, scene_pos=None):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMousePress)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.LeftButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mousePressEvent(ev)


def _move(chip, pos, scene_pos=None):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseMove)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.LeftButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mouseMoveEvent(ev)


def _release(chip, pos, scene_pos=None):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseRelease)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.NoButton)
    ev.setPos(pos)
    ev.setScenePos(scene_pos if scene_pos is not None else pos)
    chip.mouseReleaseEvent(ev)


def test_read_only_press_near_edge_does_not_enter_resize_mode(qapp):
    """A read-only chip never starts a resize_top/resize_bottom drag, even
    when pressed inside the edge zone."""
    from PySide6.QtCore import QPointF

    chip = _make_chip(read_only=True)
    rect = chip.boundingRect()
    top_pos = QPointF(rect.center().x(), rect.top() + 3)
    _press(chip, top_pos)
    assert chip._drag_mode == "pending"


def test_read_only_body_drag_emits_no_drag_committed(qapp):
    """Body drag past MOVE_THRESHOLD_PX on a read-only chip emits neither
    drag_progress nor drag_committed."""
    from PySide6.QtCore import QPointF

    chip = _make_chip(read_only=True)
    progress_calls: list = []
    committed_calls: list = []
    chip.drag_progress.connect(lambda *a: progress_calls.append(a))
    chip.drag_committed.connect(lambda *a: committed_calls.append(a))

    center = chip.boundingRect().center()
    _press(chip, center)
    _move(chip, center + QPointF(20, 5))
    _release(chip, center + QPointF(20, 5))

    assert progress_calls == []
    assert committed_calls == []


def test_read_only_context_menu_omits_edit_and_delete(qapp, monkeypatch):
    """Right-click on a read-only chip builds a menu without Edit/Delete
    entries. When the menu would otherwise be empty (no completed toggle
    available), the menu is never shown — verified by asserting QMenu.exec
    is not called."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QGraphicsSceneContextMenuEvent, QMenu

    chip = _make_chip(read_only=True)

    actions_seen: list[list[str]] = []
    exec_calls: list[QMenu] = []

    real_add_action = QMenu.addAction

    def _spy_add_action(self, text):
        return real_add_action(self, text)

    def _spy_exec(self, _pos):
        actions_seen.append(
            [a.text() for a in self.actions() if isinstance(a, QAction)]
        )
        exec_calls.append(self)
        return None

    monkeypatch.setattr(QMenu, "exec", _spy_exec)

    ev = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ev)

    # The chip's _completed_display_enabled is False by default, so the menu
    # would only contain Edit/Delete — but those are suppressed. The menu is
    # empty, so exec is short-circuited.
    assert exec_calls == []
    assert actions_seen == []


def test_writable_chip_context_menu_has_edit_and_delete(qapp, monkeypatch):
    """Sanity-check the inverse: a writable chip's menu does contain Edit
    and Delete."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QGraphicsSceneContextMenuEvent, QMenu

    chip = _make_chip(read_only=False)
    seen_actions: list[list[str]] = []

    def _spy_exec(self, _pos):
        seen_actions.append(
            [a.text() for a in self.actions() if isinstance(a, QAction)]
        )
        return None

    monkeypatch.setattr(QMenu, "exec", _spy_exec)

    ev = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ev)

    assert seen_actions, "menu should be shown on a writable chip"
    labels = seen_actions[0]
    assert "Edit…" in labels
    assert "Delete…" in labels


def test_writable_chip_body_drag_still_emits_signals(qapp):
    """Regression guard: read_only=False keeps the existing drag behavior."""
    from PySide6.QtCore import QPointF

    chip = _make_chip(read_only=False)
    committed_calls: list = []
    chip.drag_committed.connect(lambda *a: committed_calls.append(a))

    center = chip.boundingRect().center()
    _press(chip, center)
    _move(chip, center + QPointF(20, 5))
    _release(chip, center + QPointF(20, 5))
    assert len(committed_calls) == 1
