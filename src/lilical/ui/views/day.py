from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore
from lilical.ui import theme
from lilical.utils.timezone import local_iana_tz, local_zoneinfo
from lilical.ui.views._overlap import pack_overlapping
from lilical.ui.widgets.drag_preview import DragPreview
from lilical.ui.widgets.event_chip import ChipMode, EventChip

log = logging.getLogger(__name__)

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 40
ALL_DAY_ROW_H = 26
ALL_DAY_BAND_MIN = 34
ALL_DAY_MAX_ROWS = 4
DEFAULT_PX_PER_HOUR = 64
PX_PER_HOUR_MIN = 24
PX_PER_HOUR_MAX = 120
HOURS = 24

WORK_START_HOUR = 8
WORK_END_HOUR = 23
WORK_END_MINUTE = 45


def _local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


class DayGrid(QGraphicsItem):
    def __init__(
        self,
        day: date,
        width: float,
        *,
        px_per_hour: int = DEFAULT_PX_PER_HOUR,
        all_day_band_h: float = ALL_DAY_BAND_MIN,
    ) -> None:
        super().__init__()
        self._day = day
        self._width = width
        self._px_per_hour = px_per_hour
        self._all_day_band_h = all_day_band_h

    def grid_height(self) -> float:
        return DAY_HEADER_H + self._all_day_band_h + HOURS * self._px_per_hour

    def hour_top(self) -> float:
        return DAY_HEADER_H + self._all_day_band_h

    @property
    def px_per_hour(self) -> int:
        return self._px_per_hour

    @property
    def all_day_band_h(self) -> float:
        return self._all_day_band_h

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    def set_px_per_hour(self, px: int) -> None:
        self._px_per_hour = max(PX_PER_HOUR_MIN, min(PX_PER_HOUR_MAX, int(px)))
        self.prepareGeometryChange()

    def set_all_day_band_h(self, h: float) -> None:
        self._all_day_band_h = max(ALL_DAY_BAND_MIN, h)
        self.prepareGeometryChange()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, self.grid_height())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        # Body-only painter. The header + all-day band is rendered by
        # `_DayStickyHeader` so it can stay pinned during vertical scroll.
        is_today = date.today() == self._day
        body_top = self.hour_top()
        body_h = HOURS * self._px_per_hour

        # Time-axis column.
        painter.fillRect(
            QRectF(0, body_top, TIME_AXIS_WIDTH, body_h),
            QColor(theme.BG_TIME_AXIS),
        )
        # Today tint over body.
        if is_today:
            painter.fillRect(
                QRectF(
                    TIME_AXIS_WIDTH, body_top, self._width - TIME_AXIS_WIDTH, body_h
                ),
                QColor(62, 130, 246, 28),
            )

        # Off-hours dim.
        body_x = TIME_AXIS_WIDTH
        body_w = self._width - TIME_AXIS_WIDTH
        dim = QColor(0, 0, 0, 90)
        painter.fillRect(
            QRectF(body_x, body_top, body_w, WORK_START_HOUR * self._px_per_hour),
            dim,
        )
        work_end_minutes = WORK_END_HOUR * 60 + WORK_END_MINUTE
        work_end_y = body_top + work_end_minutes * self._px_per_hour / 60
        painter.fillRect(
            QRectF(body_x, work_end_y, body_w, self.grid_height() - work_end_y),
            dim,
        )

        # Grid lines (body only).
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        for hour in range(HOURS + 1):
            y = body_top + hour * self._px_per_hour
            painter.drawLine(TIME_AXIS_WIDTH, y, self._width, y)
        # Half-hour dotted lines.
        if self._px_per_hour >= 40:
            painter.setPen(
                QPen(QColor(theme.BORDER).darker(125), 1, Qt.PenStyle.DotLine)
            )
            for hour in range(HOURS):
                y = body_top + hour * self._px_per_hour + self._px_per_hour / 2
                painter.drawLine(TIME_AXIS_WIDTH, y, self._width, y)
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(TIME_AXIS_WIDTH, body_top, TIME_AXIS_WIDTH, self.grid_height())

        # Hour labels.
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_TIME_AXIS))
        for hour in range(HOURS):
            y = body_top + hour * self._px_per_hour
            painter.drawText(
                QRectF(0, y - 8, TIME_AXIS_WIDTH - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{hour:02d}:00",
            )

        # Now line.
        if is_today:
            now = datetime.now().astimezone()
            minutes = now.hour * 60 + now.minute
            ny = body_top + minutes * self._px_per_hour / 60
            painter.setPen(QPen(QColor(theme.DANGER), 2))
            painter.drawLine(TIME_AXIS_WIDTH, ny, self._width, ny)
            painter.setBrush(QColor(theme.DANGER))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(TIME_AXIS_WIDTH - 4, ny - 4, 8, 8))


class _DayStickyHeader(QGraphicsItem):
    """Floating header for Day view; pinned to the viewport on vertical scroll."""

    def __init__(
        self,
        day: date,
        width: float,
        all_day_band_h: float = ALL_DAY_BAND_MIN,
    ) -> None:
        super().__init__()
        self._day = day
        self._width = width
        self._all_day_band_h = all_day_band_h
        self.setZValue(100)

    def header_h(self) -> float:
        return DAY_HEADER_H + self._all_day_band_h

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    def set_all_day_band_h(self, h: float) -> None:
        self._all_day_band_h = max(ALL_DAY_BAND_MIN, h)
        self.prepareGeometryChange()

    def set_day(self, d: date) -> None:
        self._day = d
        self.update()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, self.header_h())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        is_today = date.today() == self._day
        all_day_band_h = self._all_day_band_h
        body_top = DAY_HEADER_H + all_day_band_h

        painter.fillRect(
            QRectF(0, 0, self._width, DAY_HEADER_H), QColor(theme.BG_SURFACE)
        )
        painter.fillRect(
            QRectF(0, DAY_HEADER_H, self._width, all_day_band_h),
            QColor(theme.BG_WEEKEND),
        )
        if is_today:
            painter.fillRect(
                QRectF(
                    TIME_AXIS_WIDTH,
                    DAY_HEADER_H,
                    self._width - TIME_AXIS_WIDTH,
                    all_day_band_h,
                ),
                QColor(62, 130, 246, 28),
            )

        # Vertical divider for the time-axis column within the header.
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(TIME_AXIS_WIDTH, 0, TIME_AXIS_WIDTH, body_top)

        # Strong borders.
        painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        painter.drawLine(0, DAY_HEADER_H, self._width, DAY_HEADER_H)
        painter.drawLine(0, body_top, self._width, body_top)

        # Header text.
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_DAY_HEADER, QFont.Weight.Bold)
        )
        painter.drawText(
            QRectF(0, 0, self._width, DAY_HEADER_H),
            Qt.AlignmentFlag.AlignCenter,
            self._day.strftime("%A, %B %-d, %Y"),
        )


class _DayCanvas(QGraphicsView):
    """Graphics canvas portion of the Day view (the time-grid)."""

    def __init__(self, store: EventStore, day: date) -> None:
        super().__init__()
        self._store = store
        self._day = day
        self._px_per_hour = DEFAULT_PX_PER_HOUR
        self._chip_mode: ChipMode = ChipMode.BARS
        self._chips: list[EventChip] = []
        # Drag-to-create / move / resize state
        self._snap_minutes: int = 15
        self._drag_kind: str | None = None
        self._drag_start_min: int | None = None
        self._drag_current_min: int | None = None
        self._drag_chip_event = None
        self._drag_chip_mode: str | None = None
        self._drag_chip_origin: tuple | None = None
        self._drag_preview: DragPreview | None = None
        self._press_scene_pos: QPointF | None = None
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Never scroll horizontally — the grid is always sized to fit the
        # viewport width so the layout doesn't get pushed wider.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(0)

        # Initial widths are placeholders — resizeEvent fills in viewport size.
        self._grid = DayGrid(self._day, 1, px_per_hour=self._px_per_hour)
        self._scene.addItem(self._grid)
        self._sticky = _DayStickyHeader(self._day, 1)
        self._scene.addItem(self._sticky)
        self._scene.setSceneRect(self._grid.boundingRect())

        # Keep the sticky header pinned to viewport-top as the user scrolls.
        self.verticalScrollBar().valueChanged.connect(self._on_v_scroll)

    @override
    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        w = self.viewport().width()
        self._grid.set_width(w)
        self._sticky.set_width(w)
        self._scene.setSceneRect(0, 0, w, self._grid.grid_height())
        self._reposition_chips()

    def _on_v_scroll(self, value: int) -> None:
        self._sticky.setY(float(value))

    @override
    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    @override
    def wheelEvent(self, event) -> None:  # noqa: ANN001
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                return
            self.set_px_per_hour(self._px_per_hour + (4 if delta > 0 else -4))
            event.accept()
            return
        super().wheelEvent(event)

    def _rebuild_grid(self) -> None:
        self._scene.removeItem(self._grid)
        w = max(1, self.viewport().width())
        self._grid = DayGrid(self._day, w, px_per_hour=self._px_per_hour)
        self._scene.addItem(self._grid)
        self._sticky.set_width(w)
        self._sticky.set_day(self._day)
        self._scene.setSceneRect(self._grid.boundingRect())

    def set_day(self, d: date) -> None:
        self._day = d
        self._rebuild_grid()
        self.refresh()
        QTimer.singleShot(0, self._scroll_to_first_event)

    def set_px_per_hour(self, px: int) -> None:
        px = max(PX_PER_HOUR_MIN, min(PX_PER_HOUR_MAX, int(px)))
        if px == self._px_per_hour:
            return
        self._px_per_hour = px
        self._grid.set_px_per_hour(px)
        self._scene.setSceneRect(self._grid.boundingRect())
        self._reposition_chips()

    def zoom_in(self) -> None:
        self.set_px_per_hour(self._px_per_hour + 8)

    def zoom_out(self) -> None:
        self.set_px_per_hour(self._px_per_hour - 8)

    def zoom_reset(self) -> None:
        self.set_px_per_hour(DEFAULT_PX_PER_HOUR)

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
        self.refresh()

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def _scroll_to_first_event(self) -> None:
        """Scroll so the day's first timed event sits just below the sticky
        header. Falls back to the work-day start when the day has no timed
        events."""
        start_dt = _local_midnight(self._day)
        end_dt = start_dt + timedelta(hours=28)
        earliest: int | None = None
        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("DayView: failed to query instances for autoscroll")
            return
        for inst in instances:
            if inst.all_day:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            if t.date() != self._day:
                continue
            m = t.hour * 60 + t.minute
            if earliest is None or m < earliest:
                earliest = m
        target_minutes = earliest if earliest is not None else WORK_START_HOUR * 60
        target_y = target_minutes * self._px_per_hour / 60
        sb = self.verticalScrollBar()
        sb.setValue(max(0, min(sb.maximum(), int(target_y - 8))))

    def refresh(self) -> None:
        # _reposition_chips() already clears its own state — just call it.
        self._reposition_chips()

    def _reposition_chips(self) -> None:
        # Idempotent: clear any previously-placed chips before re-placing.
        # Called from both refresh() and resizeEvent(), so guarding here keeps
        # us from doubling chips when the view first lays out.
        for chip in self._chips:
            parent = chip.parentItem()
            if parent is not None:
                chip.setParentItem(None)
            if chip.scene() is self._scene:
                self._scene.removeItem(chip)
        self._chips.clear()

        start_dt = _local_midnight(self._day)
        end_dt = start_dt + timedelta(hours=28)
        w = self._grid.boundingRect().width()
        col_w = max(20, w - TIME_AXIS_WIDTH)

        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("DayView: failed to query instances")
            return

        # Count all-day events for band sizing.
        all_day_count = sum(
            1 for inst in instances if inst.all_day and _is_on(inst, self._day)
        )
        rows_shown = min(all_day_count, ALL_DAY_MAX_ROWS)
        band_h = (
            ALL_DAY_BAND_MIN if rows_shown == 0 else (4 + rows_shown * ALL_DAY_ROW_H)
        )
        if abs(band_h - self._grid.all_day_band_h) > 0.5:
            self._grid.set_all_day_band_h(band_h)
            self._sticky.set_all_day_band_h(band_h)
            self._scene.setSceneRect(self._grid.boundingRect())
        else:
            self._sticky.set_all_day_band_h(band_h)

        body_top = self._grid.hour_top()
        cal_color: dict[str, str | None] = {}
        all_day_idx = 0
        timed_bucket: list[tuple[float, float, dict]] = []

        for inst in instances:
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            if t.date() != self._day:
                continue

            if inst.calendar_id not in cal_color:
                cal = self._store.get_calendar(inst.calendar_id)
                cal_color[inst.calendar_id] = cal.color if cal else None

            if inst.all_day:
                if all_day_idx >= ALL_DAY_MAX_ROWS:
                    all_day_idx += 1
                    continue
                y = DAY_HEADER_H + 2 + all_day_idx * ALL_DAY_ROW_H
                h = ALL_DAY_ROW_H - 2
                all_day_idx += 1
                chip = EventChip(
                    event,
                    QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, h),
                    calendar_color=cal_color[inst.calendar_id],
                    mode=self._chip_mode,
                    show_time_prefix=False,
                )
                self._wire_chip_signals(chip)
                chip.setParentItem(self._sticky)
                self._chips.append(chip)
                continue

            # Timed: defer to cascade layout.
            try:
                end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
            except (ValueError, TypeError):
                end_t = t
            start_min = t.hour * 60 + t.minute
            end_min = end_t.hour * 60 + end_t.minute
            if end_min <= start_min:
                end_min = start_min + 15
            timed_bucket.append(
                (
                    float(start_min),
                    float(end_min),
                    {
                        "event": event,
                        "start_dt": t,
                        "cal_color": cal_color[inst.calendar_id],
                    },
                )
            )

        # Cascade-pack timed events for this day.
        if timed_bucket:
            packed = pack_overlapping(timed_bucket)
            for (col_i, cols, xspan, payload), (start_min, end_min, _) in zip(
                packed, timed_bucket, strict=True
            ):
                sub_w = (col_w - 2) / cols
                chip_x = TIME_AXIS_WIDTH + 1 + col_i * sub_w
                chip_w = max(8.0, xspan * sub_w)
                chip_y = body_top + start_min * self._px_per_hour / 60
                chip_h = max(14.0, (end_min - start_min) * self._px_per_hour / 60)
                chip = EventChip(
                    payload["event"],
                    QRectF(chip_x, chip_y, chip_w, chip_h),
                    calendar_color=payload["cal_color"],
                    mode=self._chip_mode,
                    time_prefix=payload["start_dt"].strftime("%H:%M"),
                    show_time_prefix=True,
                )
                self._wire_chip_signals(chip)
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

    # ── Snap / public setter ──────────────────────────────────────────────

    def set_snap_minutes(self, m: int) -> None:
        if m not in (5, 10, 15, 30, 60):
            m = 15
        self._snap_minutes = m

    # ── Chip signal wiring ────────────────────────────────────────────────

    def _wire_chip_signals(self, chip: "EventChip") -> None:
        chip.edit_requested.connect(self._on_edit_requested)
        chip.delete_requested.connect(self._on_delete_requested)
        chip.drag_progress.connect(self._on_chip_drag_progress)
        chip.drag_committed.connect(self._on_chip_drag_committed)
        chip.drag_cancelled.connect(self._on_chip_drag_cancelled)

    # ── Drag geometry helpers ─────────────────────────────────────────────

    def _snap_minutes_to(self, m: float) -> int:
        snap = self._snap_minutes
        return max(0, min(1440, round(m / snap) * snap))

    def _scene_y_to_minutes(self, scene_y: float) -> float:
        body_top = self._grid.hour_top()
        return (scene_y - body_top) * 60 / max(1, self._px_per_hour)

    def _in_body_column(self, scene_x: float) -> bool:
        return scene_x >= TIME_AXIS_WIDTH

    def _compute_timed_chip_rect(self, start_min: int, end_min: int) -> QRectF:
        w = self._grid.boundingRect().width()
        col_w = max(20, w - TIME_AXIS_WIDTH)
        body_top = self._grid.hour_top()
        pph = self._px_per_hour
        y = body_top + start_min * pph / 60
        h = max(14.0, (end_min - start_min) * pph / 60)
        return QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, h)

    def _compute_allday_chip_rect(self) -> QRectF:
        w = self._grid.boundingRect().width()
        col_w = max(20, w - TIME_AXIS_WIDTH)
        scroll_y = float(self.verticalScrollBar().value())
        y = scroll_y + DAY_HEADER_H + 2
        return QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, ALL_DAY_ROW_H - 2)

    # ── View-level mouse overrides (create-flow) ──────────────────────────

    @override
    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        vp_pos = event.pos()
        vp_y = vp_pos.y()

        if vp_y < DAY_HEADER_H:
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(vp_pos)

        item = self._scene.itemAt(scene_pos, self.viewportTransform())
        if isinstance(item, EventChip):
            super().mousePressEvent(event)
            return

        if not self._in_body_column(scene_pos.x()):
            super().mousePressEvent(event)
            return

        header_h = self._sticky.header_h()

        if vp_y < header_h:
            self._drag_kind = "create_allday"
            self._press_scene_pos = scene_pos
        else:
            start_min = self._snap_minutes_to(self._scene_y_to_minutes(scene_pos.y()))
            self._drag_kind = "create_body"
            self._drag_start_min = start_min
            self._drag_current_min = start_min
            self._press_scene_pos = scene_pos
        event.accept()

    @override
    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_kind is None:
            super().mouseMoveEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())

        if self._drag_kind == "create_body":
            current_min = self._snap_minutes_to(self._scene_y_to_minutes(scene_pos.y()))
            self._drag_current_min = current_min
            start_min = min(self._drag_start_min, current_min)
            end_min = max(self._drag_start_min, current_min)
            if end_min <= start_min:
                end_min = start_min + self._snap_minutes
            rect = self._compute_timed_chip_rect(start_min, end_min)
            sh, sm = divmod(start_min, 60)
            eh, em = divmod(end_min % 1440, 60)
            duration = end_min - start_min
            dh, dm = divmod(duration, 60)
            if dh and dm:
                dur_str = f"({dh}h {dm}m)"
            elif dh:
                dur_str = f"({dh}h)"
            else:
                dur_str = f"({dm}m)"
            label = f"{sh:02d}:{sm:02d} – {eh:02d}:{em:02d}  {dur_str}"
        else:  # create_allday
            rect = self._compute_allday_chip_rect()
            label = "All day"

        if self._drag_preview is None:
            self._drag_preview = DragPreview(rect, label)
            self._scene.addItem(self._drag_preview)
        else:
            self._drag_preview.set_rect(rect)
            self._drag_preview.set_label(label)
        event.accept()

    @override
    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_kind is None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())

        if self._drag_kind == "create_body":
            current_min = self._snap_minutes_to(self._scene_y_to_minutes(scene_pos.y()))
            start_min = min(self._drag_start_min, current_min)
            end_min = max(self._drag_start_min, current_min)
            if end_min - start_min < self._snap_minutes:
                end_min = start_min + 60
            tz = local_zoneinfo()
            start_dt = datetime(
                self._day.year,
                self._day.month,
                self._day.day,
                start_min // 60,
                start_min % 60,
                tzinfo=tz,
            )
            end_dt = start_dt + timedelta(minutes=end_min - start_min)
            self._teardown_preview()
            self._drag_kind = None
            self._open_create_dialog(start_dt, end_dt, all_day=False)
        else:  # create_allday
            tz = local_zoneinfo()
            start_dt = datetime(
                self._day.year, self._day.month, self._day.day, tzinfo=tz
            )
            end_day_excl = self._day + timedelta(days=1)
            end_dt = datetime(
                end_day_excl.year, end_day_excl.month, end_day_excl.day, 0, 0, tzinfo=tz
            )
            self._teardown_preview()
            self._drag_kind = None
            self._open_create_dialog(start_dt, end_dt, all_day=True)

    @override
    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key.Key_Escape:
            if self._drag_kind is not None:
                self._teardown_preview()
                self._drag_kind = None
                self._drag_start_min = None
                self._drag_current_min = None
                self._press_scene_pos = None
                event.accept()
                return
            if self._drag_chip_event is not None:
                for chip in self._chips:
                    if chip._event is self._drag_chip_event:
                        chip.cancel_drag()
                        break
                else:
                    self._on_chip_drag_cancelled(self._drag_chip_event)
                event.accept()
                return
        super().keyPressEvent(event)

    # ── Chip drag handlers ────────────────────────────────────────────────

    def _on_chip_drag_progress(self, event, mode: str, scene_pos: QPointF) -> None:
        pph = self._px_per_hour
        body_top = self._grid.hour_top()

        if event.all_day:
            return

        if self._drag_chip_event is None:
            self._drag_chip_event = event
            self._drag_chip_mode = mode
            for chip in self._chips:
                if chip._event is event:
                    r = chip.sceneBoundingRect()
                    self._press_scene_pos = chip._press_scene_pos
                    origin_start = int((r.top() - body_top) * 60 / pph)
                    origin_end = int((r.bottom() - body_top) * 60 / pph)
                    self._drag_chip_origin = (0, origin_start, origin_end)
                    break
            else:
                return

        _origin_day, origin_start, origin_end = self._drag_chip_origin
        press = self._press_scene_pos
        duration = origin_end - origin_start

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = max(0, min(1440 - duration, new_start))
            new_end = new_start + duration
        elif mode == "resize_top":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = min(new_start, origin_end - self._snap_minutes)
            new_end = origin_end
        else:  # resize_bottom
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_end = self._snap_minutes_to(cursor_min)
            new_end = max(new_end, origin_start + self._snap_minutes)
            new_end = min(new_end, 1440)
            new_start = origin_start

        rect = self._compute_timed_chip_rect(new_start, new_end)
        sh, sm = divmod(new_start, 60)
        eh, em = divmod(new_end % 1440, 60)
        duration_shown = new_end - new_start
        dh, dm = divmod(duration_shown, 60)
        if dh and dm:
            dur_str = f"({dh}h {dm}m)"
        elif dh:
            dur_str = f"({dh}h)"
        else:
            dur_str = f"({dm}m)"
        label = f"{sh:02d}:{sm:02d} – {eh:02d}:{em:02d}  {dur_str}"

        if self._drag_preview is None:
            self._drag_preview = DragPreview(rect, label)
            self._scene.addItem(self._drag_preview)
        else:
            self._drag_preview.set_rect(rect)
            self._drag_preview.set_label(label)

    def _on_chip_drag_committed(self, event, mode: str, scene_pos: QPointF) -> None:
        import dataclasses

        if self._drag_chip_event is None or self._drag_chip_origin is None:
            self._teardown_preview()
            return
        if event.all_day:
            self._teardown_preview()
            self._drag_chip_event = None
            self._drag_chip_mode = None
            self._drag_chip_origin = None
            self._press_scene_pos = None
            return

        pph = self._px_per_hour
        _origin_day, origin_start, origin_end = self._drag_chip_origin
        press = self._press_scene_pos
        duration = origin_end - origin_start

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = max(0, min(1440 - duration, new_start))
            new_end = new_start + duration
        elif mode == "resize_top":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = min(new_start, origin_end - self._snap_minutes)
            new_end = origin_end
        else:  # resize_bottom
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_end = self._snap_minutes_to(cursor_min)
            new_end = max(new_end, origin_start + self._snap_minutes)
            new_end = min(new_end, 1440)
            new_start = origin_start

        local_tz = local_zoneinfo()
        new_dtstart = datetime(
            self._day.year,
            self._day.month,
            self._day.day,
            new_start // 60,
            new_start % 60,
            0,
            tzinfo=local_tz,
        )
        new_dtend = new_dtstart + timedelta(minutes=new_end - new_start)
        updated = dataclasses.replace(
            event,
            dtstart=new_dtstart,
            dtend=new_dtend,
            tz=local_iana_tz(),
            sequence=event.sequence + 1,
            local_dirty=True,
        )
        self._store.queue_update(updated, event.etag)
        self._teardown_preview()
        self._drag_chip_event = None
        self._drag_chip_mode = None
        self._drag_chip_origin = None
        self._press_scene_pos = None

    def _on_chip_drag_cancelled(self, event) -> None:
        self._teardown_preview()
        self._drag_chip_event = None
        self._drag_chip_mode = None
        self._drag_chip_origin = None
        self._press_scene_pos = None

    def _teardown_preview(self) -> None:
        if self._drag_preview is not None:
            self._scene.removeItem(self._drag_preview)
            self._drag_preview = None

    def _open_create_dialog(
        self, start_dt: datetime, end_dt: datetime, *, all_day: bool = False
    ) -> None:
        import uuid
        from lilical.ui.widgets.event_dialog import EventDialog

        dlg = EventDialog(
            self.parent(),
            store=self._store,
            default_dt=start_dt,
            default_dtend=end_dt,
            default_all_day=all_day,
        )
        if dlg.exec():
            new_event = dlg.build_event(str(uuid.uuid4()))
            self._store.queue_create(new_event)


def _is_on(inst, d: date) -> bool:
    try:
        return datetime.fromisoformat(inst.dtstart_local).astimezone().date() == d
    except (ValueError, TypeError):
        return False


class DayView(QWidget):
    """Day view = time-grid canvas + mini-agenda strip (next 3 upcoming)."""

    MINI_AGENDA_COUNT = 3
    MINI_AGENDA_H = 96

    def __init__(self, store: EventStore, day: date | None = None) -> None:
        super().__init__()
        self._store = store
        self._day = day or date.today()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._canvas = _DayCanvas(store, self._day)
        layout.addWidget(self._canvas, 1)

        # Mini-agenda strip below the time grid.
        self._mini_label = QLabel("Upcoming")
        self._mini_label.setStyleSheet(
            f"padding: 4px 8px; color: {theme.TEXT_SECONDARY}; "
            f"background: {theme.BG_SURFACE}; "
            f"border-top: 1px solid {theme.BORDER_STRONG};"
        )
        self._mini_label.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION, QFont.Weight.Bold)
        )
        layout.addWidget(self._mini_label)

        self._mini_list = QListWidget()
        self._mini_list.setFixedHeight(self.MINI_AGENDA_H)
        self._mini_list.setStyleSheet(
            f"background: {theme.BG_BASE}; color: {theme.TEXT_PRIMARY}; border: none;"
        )
        layout.addWidget(self._mini_list)

        # First-paint auto-scroll: wait for the viewport to have a real size.
        QTimer.singleShot(0, self._canvas._scroll_to_first_event)
        self._refresh_mini_agenda()

    # ── Public surface used by main_window / sidebar ─────────────────────

    def navigate(self, days: int) -> None:
        self._day = self._day + timedelta(days=days)
        # `set_day` already calls `_canvas._scroll_to_first_event` via QTimer.
        self._canvas.set_day(self._day)
        self._refresh_mini_agenda()

    def go_today(self) -> None:
        self._day = date.today()
        self._canvas.set_day(self._day)
        self._refresh_mini_agenda()

    def set_day(self, d: date) -> None:
        self._day = d
        self._canvas.set_day(d)
        self._refresh_mini_agenda()

    def range_label(self) -> str:
        return self._day.strftime("%A, %B %-d, %Y")

    def refresh(self) -> None:
        self._canvas.refresh()
        self._refresh_mini_agenda()

    def zoom_in(self) -> None:
        self._canvas.zoom_in()

    def zoom_out(self) -> None:
        self._canvas.zoom_out()

    def zoom_reset(self) -> None:
        self._canvas.zoom_reset()

    def set_chip_mode(self, mode: ChipMode) -> None:
        self._canvas.set_chip_mode(mode)

    @property
    def chip_mode(self) -> ChipMode:
        return self._canvas.chip_mode

    def set_snap_minutes(self, m: int) -> None:
        self._canvas.set_snap_minutes(m)

    # ── Mini-agenda ──────────────────────────────────────────────────────

    def _refresh_mini_agenda(self) -> None:
        self._mini_list.clear()
        now = datetime.now().astimezone()
        end = now + timedelta(days=14)

        try:
            instances = self._store.list_instances(
                now, end, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("DayView mini-agenda: failed to query instances")
            return

        upcoming: list[tuple[datetime, object]] = []
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            if t < now:
                continue
            upcoming.append((t, inst))
        upcoming.sort(key=lambda x: x[0])

        for t, inst in upcoming[: self.MINI_AGENDA_COUNT]:
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            if t.date() == now.date():
                when = t.strftime("Today %H:%M")
            else:
                when = t.strftime("%a %H:%M")
            if inst.all_day:
                when = t.strftime("%a") if t.date() != now.date() else "Today"
                when = f"{when}  (all day)"
            label = f"{when}    {event.summary or '(no title)'}"
            if event.location:
                label += f"   · {event.location}"
            item = QListWidgetItem(label)
            color_hint = event.color
            if not color_hint:
                cal = self._store.get_calendar(inst.calendar_id)
                color_hint = cal.color if cal else None
            if color_hint:
                c = QColor(color_hint)
                if c.isValid():
                    item.setForeground(c)
            self._mini_list.addItem(item)

        if self._mini_list.count() == 0:
            self._mini_list.addItem(QListWidgetItem("(no upcoming events)"))

    @override
    def sizeHint(self) -> QSize:
        return QSize(800, 600)
