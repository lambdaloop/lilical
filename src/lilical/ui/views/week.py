from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 32
ALL_DAY_BAND_H = 28
PX_PER_HOUR = 48
HOURS = 24


class WeekGrid:
    def __init__(self, start: date, day_count: int = 7) -> None:
        self._start = start
        self._day_count = day_count
        self._today = date.today()

    def paint(self, painter: QPainter, scene_width: float) -> None:
        col_w = (scene_width - TIME_AXIS_WIDTH) / self._day_count
        pen = QPen(QColor("#3a3a3a"))
        painter.setPen(pen)

        # Day headers
        painter.setFont(QFont("sans-serif", 9))
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            x = TIME_AXIS_WIDTH + i * col_w
            painter.drawText(
                QRectF(x, 0, col_w, DAY_HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d.strftime("%a %d"),
            )

        # Horizontal hour lines
        painter.setFont(QFont("sans-serif", 8))
        for h in range(HOURS + 1):
            y = DAY_HEADER_H + ALL_DAY_BAND_H + h * PX_PER_HOUR
            if h < HOURS:
                painter.drawText(
                    QRectF(0, y - 8, TIME_AXIS_WIDTH - 4, 16),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{h:02d}:00",
                )
            painter.drawLine(TIME_AXIS_WIDTH, y, scene_width, y)

        # Now line
        now = datetime.now()
        if now.date() == self._today:
            minutes = now.hour * 60 + now.minute
            ny = DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
            painter.setPen(QPen(QColor("#e25c5c"), 2))
            painter.drawLine(TIME_AXIS_WIDTH, ny, scene_width, ny)


class WeekView(QGraphicsView):
    def __init__(self, store: EventStore, day_count: int = 7) -> None:
        super().__init__()
        self._store = store
        self._day_count = day_count
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        self._grid = WeekGrid(week_start, day_count)

    @override
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        self._grid.paint(painter, self.viewport().width())
