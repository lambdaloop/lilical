from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore
from lilical.ui.widgets.event_chip import EventChip

log = logging.getLogger(__name__)

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 32
ALL_DAY_BAND_H = 28
PX_PER_HOUR = 48
HOURS = 24

# "Work-hours" range gets the normal background; off-hours are dimmed.
WORK_START_HOUR = 8
WORK_END_HOUR = 23
WORK_END_MINUTE = 45


def _grid_height() -> float:
    return DAY_HEADER_H + ALL_DAY_BAND_H + HOURS * PX_PER_HOUR


def _local_midnight(d: date) -> datetime:
    """Return midnight of `d` in the system local timezone as a UTC-aware datetime."""
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


class WeekGrid(QGraphicsItem):
    def __init__(self, start: date, day_count: int, width: float) -> None:
        super().__init__()
        self._start = start
        self._day_count = day_count
        self._width = width

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, _grid_height())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:
        col_w = (self._width - TIME_AXIS_WIDTH) / self._day_count
        today = date.today()
        is_today_visible = (
            self._start <= today < self._start + timedelta(days=self._day_count)
        )

        # 1. Backgrounds — painted first so the grid lines + text sit on top.
        # Header background:
        painter.fillRect(
            QRectF(0, 0, self._width, DAY_HEADER_H),
            QColor("#1a1a1a"),
        )
        # All-day band background (slightly tinted to distinguish from timed area):
        painter.fillRect(
            QRectF(0, DAY_HEADER_H, self._width, ALL_DAY_BAND_H),
            QColor("#141414"),
        )
        # Time-axis column background:
        painter.fillRect(
            QRectF(0, DAY_HEADER_H + ALL_DAY_BAND_H, TIME_AXIS_WIDTH, HOURS * PX_PER_HOUR),
            QColor("#161616"),
        )
        # Today column tint (over full height of body):
        if is_today_visible:
            col_index = (today - self._start).days
            tx = TIME_AXIS_WIDTH + col_index * col_w
            # Header strip for today
            painter.fillRect(
                QRectF(tx, 0, col_w, DAY_HEADER_H),
                QColor(62, 130, 246, 50),  # #3b82f6 @ ~20% alpha
            )
            # Body strip for today
            painter.fillRect(
                QRectF(tx, DAY_HEADER_H, col_w, ALL_DAY_BAND_H + HOURS * PX_PER_HOUR),
                QColor(62, 130, 246, 28),  # ~11% alpha
            )

        # Off-hours dim — translucent black over 00:00-08:00 and 23:45-24:00
        # in the timed area (all columns), so the active range stands out.
        hour_top = DAY_HEADER_H + ALL_DAY_BAND_H
        body_x = TIME_AXIS_WIDTH
        body_w = self._width - TIME_AXIS_WIDTH
        dim = QColor(0, 0, 0, 90)
        painter.fillRect(
            QRectF(body_x, hour_top, body_w, WORK_START_HOUR * PX_PER_HOUR),
            dim,
        )
        work_end_minutes = WORK_END_HOUR * 60 + WORK_END_MINUTE
        work_end_y = hour_top + work_end_minutes * PX_PER_HOUR / 60
        painter.fillRect(
            QRectF(body_x, work_end_y, body_w, _grid_height() - work_end_y),
            dim,
        )

        # 2. Grid lines.
        painter.setPen(QPen(QColor("#555555"), 1))
        # Horizontal hour lines:
        for h in range(HOURS + 1):
            y = DAY_HEADER_H + ALL_DAY_BAND_H + h * PX_PER_HOUR
            painter.drawLine(TIME_AXIS_WIDTH, y, self._width, y)
        # Vertical column separators:
        for i in range(self._day_count + 1):
            x = TIME_AXIS_WIDTH + i * col_w
            painter.drawLine(x, 0, x, _grid_height())
        # Time-axis right border (left edge of col 0):
        painter.drawLine(TIME_AXIS_WIDTH, 0, TIME_AXIS_WIDTH, _grid_height())

        # Stronger borders for header bottom + all-day band bottom.
        painter.setPen(QPen(QColor("#7a7a7a"), 1))
        painter.drawLine(0, DAY_HEADER_H, self._width, DAY_HEADER_H)
        painter.drawLine(
            0, DAY_HEADER_H + ALL_DAY_BAND_H, self._width, DAY_HEADER_H + ALL_DAY_BAND_H
        )

        # 3. Day-of-week headers — white, bold for today's column.
        painter.setFont(QFont("sans-serif", 10, QFont.Weight.Bold))
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            x = TIME_AXIS_WIDTH + i * col_w
            painter.setPen(
                QColor("#ffffff") if d == today else QColor("#c8c8c8")
            )
            painter.drawText(
                QRectF(x, 0, col_w, DAY_HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d.strftime("%a %-d"),
            )

        # 4. Hour labels — secondary, readable.
        painter.setPen(QColor("#c8c8c8"))
        painter.setFont(QFont("sans-serif", 9))
        for h in range(HOURS):
            y = DAY_HEADER_H + ALL_DAY_BAND_H + h * PX_PER_HOUR
            painter.drawText(
                QRectF(0, y - 8, TIME_AXIS_WIDTH - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{h:02d}:00",
            )

        # 5. Red "now" line — recomputed every paint.
        if is_today_visible:
            now = datetime.now().astimezone()
            col_index = (today - self._start).days
            x_start = TIME_AXIS_WIDTH + col_index * col_w
            x_end = x_start + col_w
            minutes = now.hour * 60 + now.minute
            ny = DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
            painter.setPen(QPen(QColor("#ff6b6b"), 2))
            painter.drawLine(x_start, ny, x_end, ny)
            # Dot on the time-axis side for visibility.
            painter.setBrush(QColor("#ff6b6b"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(TIME_AXIS_WIDTH - 4, ny - 4, 8, 8))


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

    def navigate(self, weeks: int) -> None:
        self._start = self._start + timedelta(weeks=weeks)
        self._scene.removeItem(self._grid)
        self._grid = WeekGrid(self._start, self._day_count, max(800, self.viewport().width()))
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh()

    def go_today(self) -> None:
        today = date.today()
        self._start = today - timedelta(days=today.weekday())
        self._scene.removeItem(self._grid)
        self._grid = WeekGrid(self._start, self._day_count, max(800, self.viewport().width()))
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh()

    def range_label(self) -> str:
        end = self._start + timedelta(days=self._day_count - 1)
        if self._start.month == end.month:
            return f"{self._start.strftime('%B %-d')}–{end.strftime('%-d, %Y')}"
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self) -> None:
        for chip in self._chips:
            self._scene.removeItem(chip)
        self._chips.clear()
        self._reposition_chips()

    def _reposition_chips(self) -> None:
        # Query a window extending 14h before start and 14h after end to capture
        # any timezone offset without missing events near day boundaries.
        start_dt = _local_midnight(self._start)
        end_dt = _local_midnight(self._start + timedelta(days=self._day_count))
        col_w = max(
            20, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )

        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("WeekView: failed to query instances")
            return

        cal_color: dict[str, str | None] = {}
        for inst in instances:
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            day_offset = (t.date() - self._start).days
            if day_offset < 0 or day_offset >= self._day_count:
                continue
            x = TIME_AXIS_WIDTH + day_offset * col_w

            if inst.all_day:
                y = DAY_HEADER_H + 2
                h = ALL_DAY_BAND_H - 4
            else:
                minutes = t.hour * 60 + t.minute
                y = DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
                try:
                    end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
                except (ValueError, TypeError):
                    end_t = t
                end_minutes = end_t.hour * 60 + end_t.minute
                h = max(18, (end_minutes - minutes) * PX_PER_HOUR / 60)

            if inst.calendar_id not in cal_color:
                cal = self._store.get_calendar(inst.calendar_id)
                cal_color[inst.calendar_id] = cal.color if cal else None
            chip = EventChip(
                event,
                QRectF(x + 1, y, col_w - 2, h),
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
