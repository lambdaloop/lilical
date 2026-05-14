from __future__ import annotations

from datetime import date, datetime
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore

TIME_AXIS_WIDTH = 60
HEADER_H = 40
PX_PER_HOUR = 64
HOURS = 24


class DayView(QGraphicsView):
    def __init__(self, store: EventStore, day: date | None = None) -> None:
        super().__init__()
        self._store = store
        self._day = day or date.today()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @override
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        w = self.viewport().width()
        pen = QPen(QColor("#3a3a3a"))
        painter.setPen(pen)

        painter.setFont(QFont("sans-serif", 12, QFont.Weight.Bold))
        painter.drawText(
            QRectF(0, 0, w, HEADER_H),
            Qt.AlignmentFlag.AlignCenter,
            self._day.strftime("%A, %B %d, %Y"),
        )

        painter.setFont(QFont("sans-serif", 8))
        for hour in range(HOURS + 1):
            y = HEADER_H + hour * PX_PER_HOUR
            if hour < HOURS:
                painter.drawText(
                    QRectF(0, y - 8, TIME_AXIS_WIDTH - 4, 16),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{hour:02d}:00",
                )
            painter.drawLine(TIME_AXIS_WIDTH, y, w, y)

        now = datetime.now()
        if now.date() == self._day:
            minutes = now.hour * 60 + now.minute
            ny = HEADER_H + minutes * PX_PER_HOUR / 60
            painter.setPen(QPen(QColor("#e25c5c"), 2))
            painter.drawLine(TIME_AXIS_WIDTH, ny, w, ny)
