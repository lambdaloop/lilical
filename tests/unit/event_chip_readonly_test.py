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


class _StubAction:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _StubMenu:
    """Replaces QMenu in event_chip.py for tests: collects actions without
    ever opening a real popup (which would block on QPA=offscreen).

    Method names match the Qt API (camelCase) so the production code can
    call them unmodified; the `noqa: N802` markers acknowledge the project
    lint rule preferring snake_case for new method definitions.
    """

    instances: list["_StubMenu"] = []

    def __init__(self) -> None:
        self._actions: list[_StubAction] = []
        self._exec_called = False
        _StubMenu.instances.append(self)

    def addAction(self, text: str) -> _StubAction:  # noqa: N802
        action = _StubAction(text)
        self._actions.append(action)
        return action

    def addSeparator(self) -> None:  # noqa: N802
        pass

    def actions(self) -> list[_StubAction]:
        return list(self._actions)

    def isEmpty(self) -> bool:  # noqa: N802
        return not self._actions

    def exec(self, _pos):
        self._exec_called = True
        return None


def test_read_only_context_menu_omits_edit_and_delete(qapp, monkeypatch):
    """Right-click on a read-only chip builds a menu without Edit/Delete
    entries. When the menu would otherwise be empty (no completed toggle
    available), the menu is never shown — verified by asserting `exec` was
    not invoked."""
    from PySide6.QtWidgets import QGraphicsSceneContextMenuEvent

    import lilical.ui.widgets.event_chip as event_chip_mod

    _StubMenu.instances = []
    monkeypatch.setattr(event_chip_mod, "QMenu", _StubMenu)

    chip = _make_chip(read_only=True)
    ev = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ev)

    assert len(_StubMenu.instances) == 1
    menu = _StubMenu.instances[0]
    assert menu.actions() == []
    assert menu._exec_called is False


def test_writable_chip_context_menu_has_edit_and_delete(qapp, monkeypatch):
    """Sanity-check the inverse: a writable chip's menu does contain Edit
    and Delete."""
    from PySide6.QtWidgets import QGraphicsSceneContextMenuEvent

    import lilical.ui.widgets.event_chip as event_chip_mod

    _StubMenu.instances = []
    monkeypatch.setattr(event_chip_mod, "QMenu", _StubMenu)

    chip = _make_chip(read_only=False)
    ev = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ev)

    assert len(_StubMenu.instances) == 1
    menu = _StubMenu.instances[0]
    labels = [a.text() for a in menu.actions()]
    assert "Edit…" in labels
    assert "Delete…" in labels
    assert menu._exec_called is True


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
