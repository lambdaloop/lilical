"""Tests for EventChip's external signal contract from a view's perspective.

`chip_drag_test.py` covers chip-internal drag mechanics with synthesised
events; `event_chip_readonly_test.py` covers the read-only branches of the
context menu.  This file rounds out the picture with the *signal* surface
that a view sees when wiring a chip in:

- A left click (no drag) emits `details_requested` exactly once.
- A left double-click is swallowed (no extra `details_requested`/`edit_requested`).
- Selecting "Edit…" in the context menu emits `edit_requested`.
- Selecting "Delete…" emits `delete_requested`.

Right-click goes through `contextMenuEvent` which calls `menu.exec()`
(blocking on a real `QMenu`).  We replace `QMenu` in `event_chip` with a
stub that auto-returns a configurable action — same pattern as
`event_chip_readonly_test.py`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtWidgets import QGraphicsSceneContextMenuEvent, QGraphicsSceneMouseEvent

from tests.unit.conftest import make_event


def _make_chip():
    from lilical.ui.widgets.event_chip import ChipMode, EventChip

    ev = make_event("u1", hour=9, minute=0, summary="Test event")
    rect = QRectF(0, 0, 120, 40)
    chip = EventChip(
        ev,
        rect,
        calendar_color="#3498db",
        mode=ChipMode.TEXT,
        show_time_prefix=True,
        time_prefix="09:00",
        time_format="24h",
        instance_dtstart=ev.dtstart,
        completed=False,
        inst_key=None,
        read_only=False,
    )
    return chip, ev


def _press(chip, pos):
    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMousePress)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.LeftButton)
    ev.setPos(pos)
    ev.setScenePos(pos)
    chip.mousePressEvent(ev)


def _release(chip, pos):
    ev = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseRelease)
    ev.setButton(Qt.MouseButton.LeftButton)
    ev.setButtons(Qt.MouseButton.NoButton)
    ev.setPos(pos)
    ev.setScenePos(pos)
    chip.mouseReleaseEvent(ev)


# ─── Stub menu (auto-returns a chosen action) ─────────────────────────────


class _StubAction:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _StubMenu:
    """Stand-in for QMenu in event_chip.  Auto-returns an action by text."""

    target_text: str | None = None  # set by tests before contextMenuEvent

    def __init__(self) -> None:
        self._actions: list[_StubAction] = []

    def addAction(self, text: str) -> _StubAction:  # noqa: N802
        a = _StubAction(text)
        self._actions.append(a)
        return a

    def addSeparator(self) -> None:  # noqa: N802
        pass

    def actions(self) -> list[_StubAction]:
        return list(self._actions)

    def isEmpty(self) -> bool:  # noqa: N802
        return not self._actions

    def exec(self, _pos):
        for a in self._actions:
            if a.text() == _StubMenu.target_text:
                return a
        return None


def test_single_click_emits_details_requested(qapp) -> None:
    """A press+release inside the chip emits details_requested(event)."""
    chip, event = _make_chip()
    received: list = []
    chip.details_requested.connect(received.append)

    center = chip.boundingRect().center()
    _press(chip, center)
    _release(chip, center)
    qapp.processEvents()

    assert received == [event], (
        f"expected one details_requested with {event}, got {received}"
    )


def test_left_double_click_is_swallowed(qapp) -> None:
    """mouseDoubleClickEvent swallows left double-clicks (no extra signals)."""
    chip, _ = _make_chip()
    seen: list[str] = []
    chip.details_requested.connect(lambda _e: seen.append("details"))
    chip.edit_requested.connect(lambda _e: seen.append("edit"))

    dbl = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseDoubleClick)
    dbl.setButton(Qt.MouseButton.LeftButton)
    dbl.setButtons(Qt.MouseButton.LeftButton)
    dbl.setPos(QPointF(20, 20))
    chip.mouseDoubleClickEvent(dbl)
    qapp.processEvents()

    assert seen == [], (
        f"left double-click should not emit any signal, got {seen}"
    )


def test_right_click_edit_action_emits_edit_requested(qapp, monkeypatch) -> None:
    """Right-click → contextMenu → choose Edit → emits edit_requested(event)."""
    import lilical.ui.widgets.event_chip as event_chip_mod

    chip, event = _make_chip()

    _StubMenu.target_text = "Edit…"
    monkeypatch.setattr(event_chip_mod, "QMenu", _StubMenu)

    captured: list = []
    chip.edit_requested.connect(captured.append)

    ctx = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ctx)
    qapp.processEvents()

    assert captured == [event], (
        f"expected edit_requested with {event}, got {captured}"
    )


def test_right_click_delete_action_emits_delete_requested(qapp, monkeypatch) -> None:
    """Right-click → contextMenu → choose Delete → emits delete_requested(event)."""
    import lilical.ui.widgets.event_chip as event_chip_mod

    chip, event = _make_chip()

    _StubMenu.target_text = "Delete…"
    monkeypatch.setattr(event_chip_mod, "QMenu", _StubMenu)

    captured: list = []
    chip.delete_requested.connect(captured.append)

    ctx = QGraphicsSceneContextMenuEvent(
        QGraphicsSceneContextMenuEvent.Type.GraphicsSceneContextMenu
    )
    chip.contextMenuEvent(ctx)
    qapp.processEvents()

    assert captured == [event], (
        f"expected delete_requested with {event}, got {captured}"
    )
