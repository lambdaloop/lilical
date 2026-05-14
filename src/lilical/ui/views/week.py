from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore
from lilical.ui.widgets.event_chip import EventChip

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 32
ALL_DAY_BAND_H = 28
PX_PER_HOUR = 48
HOURS = 24


def _grid_height() -> float:
    return DAY_HEADER_H + ALL_DAY_BAND_H + HOURS * PX_PER_HOUR


class WeekGrid(QGraphicsItem):
    def __init__(self, start: date, day_count: int, width: float) -> None:
        super().__init__()
        self._start = start
        self._day_count = day_count
        self._width = width
        self._today = date.today()

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, _grid_height())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:
        col_w = (self._width - TIME_AXIS_WIDTH) / self._day_count
        pen = QPen(QColor("#3a3a3a"))
        painter.setPen(pen)

        painter.setFont(QFont("sans-serif", 9))
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            x = TIME_AXIS_WIDTH + i * col_w
            painter.drawText(
                QRectF(x, 0, col_w, DAY_HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d.strftime("%a %d"),
            )

        painter.setFont(QFont("sans-serif", 8))
        for h in range(HOURS + 1):
            y = DAY_HEADER_H + ALL_DAY_BAND_H + h * PX_PER_HOUR
            if h < HOURS:
                painter.drawText(
                    QRectF(0, y - 8, TIME_AXIS_WIDTH - 4, 16),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{h:02d}:00",
                )
            painter.drawLine(TIME_AXIS_WIDTH, y, self._width, y)

        now = datetime.now()
        if now.date() == self._today:
            minutes = now.hour * 60 + now.minute
            ny = DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
            painter.setPen(QPen(QColor("#e25c5c"), 2))
            painter.drawLine(TIME_AXIS_WIDTH, ny, self._width, ny)


class WeekView(QGraphicsView):
    def __init__(self, store: EventStore, day_count: int = 7) -> None:
        super().__init__()
        self._store = store
        self._day_count = day_count
        self._chips: list[EventChip] = []
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        self._start = week_start
        self._grid = WeekGrid(week_start, day_count, 800)
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
            self._start.year, self._start.month, self._start.day, tzinfo=timezone.utc
        )
        end_dt = start_dt + timedelta(days=self._day_count)
        col_w = max(
            20, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )

        for inst in self._store.list_instances(start_dt, end_dt):
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local)
            except (ValueError, TypeError):
                continue
            day_offset = (t.date() - self._start).days
            if day_offset < 0 or day_offset >= self._day_count:
                continue
            minutes = t.hour * 60 + t.minute
            x = TIME_AXIS_WIDTH + day_offset * col_w
            y = DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
            try:
                end_t = datetime.fromisoformat(inst.dtend_local)
            except (ValueError, TypeError):
                end_t = t
            end_minutes = end_t.hour * 60 + end_t.minute
            h = max(18, (end_minutes - minutes) * PX_PER_HOUR / 60)
            if inst.all_day:
                y = DAY_HEADER_H + 2
                h = ALL_DAY_BAND_H - 4

            chip = EventChip(event, QRectF(x + 1, y, col_w - 2, h))
            self._scene.addItem(chip)
            self._chips.append(chip)
