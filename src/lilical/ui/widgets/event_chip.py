from __future__ import annotations

from typing import TYPE_CHECKING, override

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsObject, QGraphicsSceneContextMenuEvent, QMenu

if TYPE_CHECKING:
    from lilical.models.event import Event

_FALLBACK_COLOR = "#9ec5ff"


def _resolve_color(event_color: str | None, fallback: str | None) -> QColor:
    """Pick the first valid colour from (event-own, calendar-fallback, default)."""
    for candidate in (event_color, fallback, _FALLBACK_COLOR):
        if not candidate:
            continue
        c = QColor(candidate)
        if c.isValid():
            return c
    return QColor(_FALLBACK_COLOR)


def _readable_text_color(bg: QColor) -> QColor:
    luminance = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
    return QColor("#000000") if luminance > 0.55 else QColor("#ffffff")


class EventChip(QGraphicsObject):
    """Colored rectangle representing one calendar event in a graphics view."""

    edit_requested = Signal(object)    # emits Event
    delete_requested = Signal(object)  # emits Event

    def __init__(
        self,
        event: "Event",
        rect: QRectF,
        *,
        calendar_color: str | None = None,
    ) -> None:
        super().__init__()
        self._event = event
        self._rect = rect
        self._calendar_color = calendar_color
        self._hovered = False
        self.setAcceptHoverEvents(True)
        self.setToolTip(self._build_tooltip())

    def _build_tooltip(self) -> str:
        parts = [self._event.summary or "(no title)"]
        if self._event.location:
            parts.append(self._event.location)
        return "\n".join(parts)

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:
        base = _resolve_color(self._event.color, self._calendar_color)
        if self._hovered:
            base = base.lighter(130)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(base)
        painter.setPen(QPen(base.darker(140), 0))
        painter.drawRoundedRect(self._rect.adjusted(0, 0, -1, -1), 4, 4)

        text_color = _readable_text_color(base)
        painter.setPen(text_color)
        painter.setFont(QFont("sans-serif", 8, QFont.Weight.Medium))
        clip = self._rect.adjusted(4, 2, -4, -2)
        painter.setClipRect(clip)
        painter.drawText(
            clip,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._event.summary or "",
        )

    def hoverEnterEvent(self, event) -> None:  # noqa: ANN001
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: ANN001
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self._event)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    @override
    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        edit_act = menu.addAction("Edit…")
        menu.addSeparator()
        del_act = menu.addAction("Delete…")
        chosen = menu.exec(event.screenPos())
        if chosen is edit_act:
            self.edit_requested.emit(self._event)
        elif chosen is del_act:
            self.delete_requested.emit(self._event)
