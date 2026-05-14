from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore
from lilical.ui.widgets.event_chip import EventChip

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 40
PX_PER_HOUR = 64
HOURS = 24


def _grid_height() -> float:
    return DAY_HEADER_H + HOURS * PX_PER_HOUR


class DayGrid(QGraphicsItem):
    def __init__(self, day: date, width: float) -> None:
        super().__init__()
        self._day = day
        self._width = width

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, _grid_height())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:
        pen = QPen(QColor("#3a3a3a"))
        painter.setPen(pen)

        painter.setFont(QFont("sans-serif", 12, QFont.Weight.Bold))
        painter.drawText(
            QRectF(0, 0, self._width, DAY_HEADER_H),
            Qt.AlignmentFlag.AlignCenter,
            self._day.strftime("%A, %B %d, %Y"),
        )

        painter.setFont(QFont("sans-serif", 8))
        for hour in range(HOURS + 1):
            y = DAY_HEADER_H + hour * PX_PER_HOUR
            if hour < HOURS:
                painter.drawText(
                    QRectF(0, y - 8, TIME_AXIS_WIDTH - 4, 16),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{hour:02d}:00",
                )
            painter.drawLine(TIME_AXIS_WIDTH, y, self._width, y)

        now = datetime.now()
        if now.date() == self._day:
            minutes = now.hour * 60 + now.minute
            ny = DAY_HEADER_H + minutes * PX_PER_HOUR / 60
            painter.setPen(QPen(QColor("#e25c5c"), 2))
            painter.drawLine(TIME_AXIS_WIDTH, ny, self._width, ny)


class DayView(QGraphicsView):
    def __init__(self, store: EventStore, day: date | None = None) -> None:
        super().__init__()
        self._store = store
        self._day = day or date.today()
        self._chips: list[EventChip] = []
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._grid = DayGrid(self._day, 800)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh()

    @override
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w = self.viewport().width()
        self._grid.set_width(w)
        self._scene.setSceneRect(0, 0, w, _grid_height())
        self._reposition_chips()

    def refresh(self) -> None:
        for chip in self._chips:
            self._scene.removeItem(chip)
        self._chips.clear()
        self._reposition_chips()

    def _reposition_chips(self) -> None:
        start_dt = datetime(
            self._day.year, self._day.month, self._day.day, tzinfo=timezone.utc
        )
        end_dt = start_dt + timedelta(days=1)
        w = self._grid.boundingRect().width()
        col_w = max(20, w - TIME_AXIS_WIDTH)

        for inst in self._store.list_instances(start_dt, end_dt):
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local)
            except (ValueError, TypeError):
                continue
            minutes = t.hour * 60 + t.minute
            y = DAY_HEADER_H + minutes * PX_PER_HOUR / 60
            try:
                end_t = datetime.fromisoformat(inst.dtend_local)
            except (ValueError, TypeError):
                end_t = t
            end_minutes = end_t.hour * 60 + end_t.minute
            h = max(18, (end_minutes - minutes) * PX_PER_HOUR / 60)
            if inst.all_day:
                y = DAY_HEADER_H + 2
                h = 26

            chip = EventChip(event, QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, h))
            self._scene.addItem(chip)
            self._chips.append(chip)
