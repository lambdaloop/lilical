from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QSizePolicy,
)

from lilical.storage.event_store import EventStore
from lilical.ui.widgets.event_chip import EventChip

log = logging.getLogger(__name__)

CELL_W = 140
CELL_H = 100
HEADER_H = 24
COLS = 7
ROWS = 6
PAD = 4
CHIP_H = 16
CHIP_GAP = 1


def _local_midnight(d: date) -> datetime:
    """Return midnight of `d` in the system local timezone as a UTC-aware datetime."""
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


class MonthGrid(QGraphicsItem):
    def __init__(self, year: int, month: int) -> None:
        super().__init__()
        self._year = year
        self._month = month
        self._first = date(year, month, 1)
        if month == 12:
            self._last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            self._last = date(year, month + 1, 1) - timedelta(days=1)
        self._start = self._first - timedelta(days=self._first.weekday())
        self._today = date.today()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, COLS * CELL_W, HEADER_H + ROWS * CELL_H)

    def cell_rect(self, day: date) -> QRectF | None:
        # Grid has 42 cells (6 rows × 7 cols), indices 0..41; >= 42 is out-of-bounds.
        if day < self._start or day >= self._start + timedelta(days=42):
            return None
        offset = (day - self._start).days
        c = offset % 7
        r = offset // 7
        return QRectF(c * CELL_W, HEADER_H + r * CELL_H, CELL_W, CELL_H)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        painter.setFont(QFont("sans-serif", 9))
        for i, d in enumerate(days):
            painter.drawText(
                QRectF(i * CELL_W, 0, CELL_W, HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d,
            )

        pen = QPen(QColor("#555555"))
        painter.setPen(pen)
        for r in range(ROWS + 1):
            y = HEADER_H + r * CELL_H
            painter.drawLine(0, y, COLS * CELL_W, y)
        for c in range(COLS + 1):
            x = c * CELL_W
            painter.drawLine(x, HEADER_H, x, HEADER_H + ROWS * CELL_H)

        painter.setFont(QFont("sans-serif", 10, QFont.Weight.Bold))
        cur = self._start
        for r in range(ROWS):
            for c in range(COLS):
                x = c * CELL_W + PAD
                y = HEADER_H + r * CELL_H + PAD
                in_month = self._first <= cur <= self._last
                painter.setPen(QColor("#c8c8c8") if not in_month else QColor("#ffffff"))
                if cur == self._today and in_month:
                    painter.setBrush(QColor("#3b82f6"))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(x, y, 20, 20)
                    painter.setPen(QColor("#ffffff"))
                painter.drawText(x + 2, y + 14, str(cur.day))
                cur += timedelta(days=1)


class MonthView(QGraphicsView):
    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        self._chips: list[EventChip] = []
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        now = date.today()
        self._year = now.year
        self._month = now.month
        self._grid = MonthGrid(now.year, now.month)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh()

    @override
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._scene.setSceneRect(self._grid.boundingRect())

    def _rebuild_grid(self) -> None:
        self._scene.removeItem(self._grid)
        self._grid = MonthGrid(self._year, self._month)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())

    def navigate(self, months: int) -> None:
        m = self._month + months
        y = self._year
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        self._year, self._month = y, m
        self._rebuild_grid()
        self.refresh()

    def go_today(self) -> None:
        today = date.today()
        self._year, self._month = today.year, today.month
        self._rebuild_grid()
        self.refresh()

    def range_label(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def refresh(self) -> None:
        for chip in self._chips:
            self._scene.removeItem(chip)
        self._chips.clear()

        start_dt = _local_midnight(date(self._year, self._month, 1))
        if self._month == 12:
            end_dt = _local_midnight(date(self._year + 1, 1, 1))
        else:
            end_dt = _local_midnight(date(self._year, self._month + 1, 1))

        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("MonthView: failed to query instances")
            return

        insts_by_day: dict[date, list] = {}
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            insts_by_day.setdefault(t.date(), []).append(inst)

        cal_color: dict[str, str | None] = {}
        for day, instances_on_day in insts_by_day.items():
            cell = self._grid.cell_rect(day)
            if cell is None:
                continue
            if day.month != self._month:
                continue
            max_chips = max(0, int((cell.height() - 20) / (CHIP_H + CHIP_GAP)))
            for i, inst in enumerate(instances_on_day[:max_chips]):
                event = self._store.get_event(inst.uid, inst.calendar_id)
                if event is None:
                    continue
                cx = cell.x() + 2
                cy = cell.y() + 20 + i * (CHIP_H + CHIP_GAP)
                if inst.calendar_id not in cal_color:
                    cal = self._store.get_calendar(inst.calendar_id)
                    cal_color[inst.calendar_id] = cal.color if cal else None
                chip = EventChip(
                    event,
                    QRectF(cx, cy, cell.width() - 4, CHIP_H),
                    calendar_color=cal_color[inst.calendar_id],
                )
                chip.edit_requested.connect(self._on_edit_requested)
                chip.delete_requested.connect(self._on_delete_requested)
                self._scene.addItem(chip)
                self._chips.append(chip)

    def _on_edit_requested(self, event) -> None:
        from lilical.ui.widgets.event_dialog import EventDialog

        dlg = EventDialog(self.parent(), store=self._store, event=event)
        if dlg.exec():
            import dataclasses
            updated = dataclasses.replace(
                dlg.build_event(event.uid),
                calendar_id=dlg.calendar_id or event.calendar_id,
                etag=event.etag,
                sequence=event.sequence + 1,
            )
            self._store.queue_update(updated, event.etag)

    def _on_delete_requested(self, event) -> None:
        from PySide6.QtWidgets import QMessageBox

        if (
            QMessageBox.question(
                self.parent(),
                "Delete event",
                f'Delete "{event.summary}"?',
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._store.queue_delete(event.uid, event.calendar_id)
