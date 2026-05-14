from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from lilical.models.event import Event


class EventChip(QGraphicsItem):
    def __init__(self, event: Event, rect: QRectF) -> None:
        super().__init__()
        self._event = event
        self._rect = rect

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, painter: QPainter, option, widget=None) -> None:
        r = self._rect
        color = QColor(self._event.color or "#5e9fff")
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(r, 4, 4)

        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("sans-serif", 8, QFont.Weight.Bold))
        painter.drawText(
            r.adjusted(4, 2, -4, -2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._event.summary,
        )
