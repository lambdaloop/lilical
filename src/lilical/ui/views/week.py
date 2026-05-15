from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore
from lilical.ui import theme
from lilical.ui.views._overlap import pack_overlapping
from lilical.ui.widgets.drag_preview import DragPreview
from lilical.ui.widgets.event_chip import ChipMode, EventChip
from lilical.utils.timezone import local_iana_tz, local_zoneinfo

log = logging.getLogger(__name__)

TIME_AXIS_WIDTH = 60
DAY_HEADER_H = 32
ALL_DAY_ROW_H = 22  # one row inside the all-day band
ALL_DAY_BAND_MIN = 28  # always-reserved minimum band height
ALL_DAY_MAX_ROWS = 4  # spec §4: max 4 rows before scrolling
DEFAULT_PX_PER_HOUR = 48
PX_PER_HOUR_MIN = 20
PX_PER_HOUR_MAX = 96
HOURS = 24

VALID_DAY_COUNTS = (1, 2, 3, 4, 5, 7, 10, 14)

# "Work-hours" range gets the normal background; off-hours are dimmed.
WORK_START_HOUR = 8
WORK_END_HOUR = 23
WORK_END_MINUTE = 45


def _local_midnight(d: date) -> datetime:
    """Return midnight of `d` in the system local timezone as a UTC-aware datetime."""
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


class WeekGrid(QGraphicsItem):
    def __init__(
        self,
        start: date,
        day_count: int,
        width: float,
        *,
        px_per_hour: int = DEFAULT_PX_PER_HOUR,
        all_day_band_h: float = ALL_DAY_BAND_MIN,
    ) -> None:
        super().__init__()
        self._start = start
        self._day_count = day_count
        self._width = width
        self._px_per_hour = px_per_hour
        self._all_day_band_h = all_day_band_h

    def grid_height(self) -> float:
        return DAY_HEADER_H + self._all_day_band_h + HOURS * self._px_per_hour

    def hour_top(self) -> float:
        return DAY_HEADER_H + self._all_day_band_h

    def set_width(self, w: float) -> None:
        self._width = w
        self.prepareGeometryChange()

    def set_px_per_hour(self, px: int) -> None:
        self._px_per_hour = max(PX_PER_HOUR_MIN, min(PX_PER_HOUR_MAX, int(px)))
        self.prepareGeometryChange()

    def set_all_day_band_h(self, h: float) -> None:
        self._all_day_band_h = max(ALL_DAY_BAND_MIN, h)
        self.prepareGeometryChange()

    @property
    def px_per_hour(self) -> int:
        return self._px_per_hour

    @property
    def all_day_band_h(self) -> float:
        return self._all_day_band_h

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, self.grid_height())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        # Body-only painter. The day-of-week headers and all-day band are
        # rendered by `_StickyHeader` so they can stay pinned to the viewport
        # top while the body scrolls underneath.
        col_w = (self._width - TIME_AXIS_WIDTH) / self._day_count
        today = date.today()
        is_today_visible = (
            self._start <= today < self._start + timedelta(days=self._day_count)
        )
        all_day_band_h = self._all_day_band_h
        body_top = DAY_HEADER_H + all_day_band_h
        body_h = HOURS * self._px_per_hour

        # 1. Backgrounds (body only).
        painter.fillRect(
            QRectF(0, body_top, TIME_AXIS_WIDTH, body_h),
            QColor(theme.BG_TIME_AXIS),
        )

        # Weekend column tint over the body.
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            if d.weekday() >= 5:
                x = TIME_AXIS_WIDTH + i * col_w
                painter.fillRect(
                    QRectF(x, body_top, col_w, body_h),
                    QColor(20, 20, 20, 60),
                )

        # Today column tint over the body.
        if is_today_visible:
            col_index = (today - self._start).days
            tx = TIME_AXIS_WIDTH + col_index * col_w
            painter.fillRect(
                QRectF(tx, body_top, col_w, body_h),
                QColor(62, 130, 246, 28),
            )

        # 2. Off-hours dim.
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

        # 3. Grid lines (body only).
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        for h in range(HOURS + 1):
            y = body_top + h * self._px_per_hour
            painter.drawLine(int(TIME_AXIS_WIDTH), int(y), int(self._width), int(y))
        # Half-hour marks (lighter) when there's enough vertical room.
        if self._px_per_hour >= 36:
            painter.setPen(
                QPen(QColor(theme.BORDER).darker(125), 1, Qt.PenStyle.DotLine)
            )
            for h in range(HOURS):
                y = body_top + h * self._px_per_hour + self._px_per_hour / 2
                painter.drawLine(int(TIME_AXIS_WIDTH), int(y), int(self._width), int(y))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        for i in range(self._day_count + 1):
            x = TIME_AXIS_WIDTH + i * col_w
            painter.drawLine(int(x), int(body_top), int(x), int(self.grid_height()))
        painter.drawLine(
            int(TIME_AXIS_WIDTH),
            int(body_top),
            int(TIME_AXIS_WIDTH),
            int(self.grid_height()),
        )

        # 4. Hour labels.
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_TIME_AXIS))
        for h in range(HOURS):
            y = body_top + h * self._px_per_hour
            painter.drawText(
                QRectF(0, y - 8, TIME_AXIS_WIDTH - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{h:02d}:00",
            )

        # 5. Now-line.
        if is_today_visible:
            now = datetime.now().astimezone()
            col_index = (today - self._start).days
            x_start = TIME_AXIS_WIDTH + col_index * col_w
            x_end = x_start + col_w
            minutes = now.hour * 60 + now.minute
            ny = body_top + minutes * self._px_per_hour / 60
            painter.setPen(QPen(QColor(theme.DANGER), 2))
            painter.drawLine(int(x_start), int(ny), int(x_end), int(ny))
            painter.setBrush(QColor(theme.DANGER))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(TIME_AXIS_WIDTH - 4, ny - 4, 8, 8))


class _StickyHeader(QGraphicsItem):
    """Floating header containing day-of-week labels and the all-day band.

    Repositioned each frame to scrollbar.value() so it appears pinned to the
    top of the viewport. All-day chips and overflow markers are parented to
    this item so they translate with it.
    """

    def __init__(
        self,
        start: date,
        day_count: int,
        width: float,
        all_day_band_h: float = ALL_DAY_BAND_MIN,
    ) -> None:
        super().__init__()
        self._start = start
        self._day_count = day_count
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

    def set_start(self, d: date) -> None:
        self._start = d
        self.update()

    def set_day_count(self, n: int) -> None:
        self._day_count = n
        self.update()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._width, self.header_h())

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        col_w = (self._width - TIME_AXIS_WIDTH) / self._day_count
        today = date.today()
        all_day_band_h = self._all_day_band_h
        body_top = DAY_HEADER_H + all_day_band_h

        painter.fillRect(
            QRectF(0, 0, self._width, DAY_HEADER_H), QColor(theme.BG_SURFACE)
        )
        painter.fillRect(
            QRectF(0, DAY_HEADER_H, self._width, all_day_band_h),
            QColor(theme.BG_WEEKEND),
        )

        # Weekend / today column tint within the header zone.
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            if d.weekday() >= 5:
                x = TIME_AXIS_WIDTH + i * col_w
                painter.fillRect(
                    QRectF(x, DAY_HEADER_H, col_w, all_day_band_h),
                    QColor(20, 20, 20, 60),
                )
        if self._start <= today < self._start + timedelta(days=self._day_count):
            col_index = (today - self._start).days
            tx = TIME_AXIS_WIDTH + col_index * col_w
            painter.fillRect(
                QRectF(tx, 0, col_w, DAY_HEADER_H),
                QColor(62, 130, 246, 50),
            )
            painter.fillRect(
                QRectF(tx, DAY_HEADER_H, col_w, all_day_band_h),
                QColor(62, 130, 246, 28),
            )

        # Vertical column dividers.
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        for i in range(self._day_count + 1):
            x = TIME_AXIS_WIDTH + i * col_w
            painter.drawLine(int(x), 0, int(x), int(body_top))
        painter.drawLine(int(TIME_AXIS_WIDTH), 0, int(TIME_AXIS_WIDTH), int(body_top))

        # Strong borders under header row and under all-day band.
        painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        painter.drawLine(0, int(DAY_HEADER_H), int(self._width), int(DAY_HEADER_H))
        painter.drawLine(0, int(body_top), int(self._width), int(body_top))

        # Day-of-week labels.
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_DAY_NUMBER, QFont.Weight.Bold)
        )
        for i in range(self._day_count):
            d = self._start + timedelta(days=i)
            x = TIME_AXIS_WIDTH + i * col_w
            painter.setPen(
                QColor(theme.TEXT_PRIMARY)
                if d == today
                else QColor(theme.TEXT_SECONDARY)
            )
            painter.drawText(
                QRectF(x, 0, col_w, DAY_HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d.strftime("%a %-d"),
            )


class WeekView(QGraphicsView):
    def __init__(self, store: EventStore, day_count: int = 7) -> None:
        super().__init__()
        self._store = store
        self._day_count = day_count if day_count in VALID_DAY_COUNTS else 7
        self._px_per_hour = DEFAULT_PX_PER_HOUR
        self._chip_mode: ChipMode = ChipMode.BARS
        self._time_format: str = "24h"
        self._chips: list[EventChip] = []
        # Drag-to-create / move / resize state
        self._snap_minutes: int = 15
        # Active drag (either originated on empty grid or on a chip)
        self._drag_kind: str | None = None
        # "create_body" → drag-to-create timed event in body
        # "create_allday" → drag-to-create all-day event
        # "chip" → drag originated on a chip (move/resize)
        self._drag_day_offset: int | None = None
        self._drag_start_min: int | None = None
        self._drag_current_min: int | None = None
        self._drag_end_day_offset: int | None = None
        self._drag_chip_event = None  # the Event being dragged
        self._drag_chip_mode: str | None = (
            None  # "move" / "resize_top" / "resize_bottom"
        )
        # (day_off, start_min, end_min)
        self._drag_chip_origin: tuple[int, int, int] | None = None
        self._drag_preview: DragPreview | None = None
        self._press_scene_pos: QPointF | None = None
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Never scroll horizontally — the grid is always sized to fit the
        # viewport width so the parent layout doesn't get pushed wider when
        # the day count changes.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Allow the layout to shrink us to nothing horizontally; our scene
        # always tracks viewport width, so contributing a wide minimum-size
        # hint here would needlessly push the main window wider.
        self.setMinimumWidth(0)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        self._start = week_start
        # Initial width is a temporary value — resizeEvent will set the real
        # viewport width as soon as the widget is laid out.
        self._grid = WeekGrid(
            week_start, self._day_count, 1, px_per_hour=self._px_per_hour
        )
        self._scene.addItem(self._grid)
        self._sticky = _StickyHeader(week_start, self._day_count, 1)
        self._scene.addItem(self._sticky)
        self._scene.setSceneRect(self._grid.boundingRect())

        # Pin the sticky header to the viewport top as the user scrolls.
        self.verticalScrollBar().valueChanged.connect(self._on_v_scroll)

        # Defer to the next tick so the viewport has a real size by then.
        QTimer.singleShot(0, self._scroll_to_first_event)

    # ── Geometry helpers ─────────────────────────────────────────────────

    def _rebuild_grid(self) -> None:
        self._scene.removeItem(self._grid)
        # Use the current viewport width — never wider — so the day count
        # change doesn't push the window wider.
        w = max(1, self.viewport().width())
        self._grid = WeekGrid(
            self._start,
            self._day_count,
            w,
            px_per_hour=self._px_per_hour,
        )
        self._scene.addItem(self._grid)
        self._sticky.set_width(w)
        self._sticky.set_start(self._start)
        self._sticky.set_day_count(self._day_count)
        self._scene.setSceneRect(self._grid.boundingRect())

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
        # Don't impose a horizontal floor on the parent layout. The scene
        # tracks viewport width — we never need to be wider than the parent
        # offers us.
        return QSize(0, 0)

    @override
    def sizeHint(self) -> QSize:
        return QSize(0, 0)

    # ── Public setters ───────────────────────────────────────────────────

    def set_day_count(self, n: int) -> None:
        if n not in VALID_DAY_COUNTS or n == self._day_count:
            return
        self._day_count = n
        self._rebuild_grid()
        self.refresh()
        QTimer.singleShot(0, self._scroll_to_first_event)

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
        self.refresh()

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def set_time_format(self, fmt: str) -> None:
        if fmt == self._time_format:
            return
        self._time_format = fmt
        self.refresh()

    def set_snap_minutes(self, m: int) -> None:
        """Set the snap granularity used by every drag interaction.

        Clamped to the values exposed in Preferences (5/10/15/30/60 min).
        """
        if m not in (5, 10, 15, 30, 60):
            m = 15
        self._snap_minutes = m

    @property
    def day_count(self) -> int:
        return self._day_count

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

    # ── Auto-scroll to first event ──────────────────────────────────────

    def _scroll_to_first_event(self) -> None:
        """Scroll so the earliest timed event of the visible range sits just
        below the sticky header. Falls back to the work-day start when there
        are no timed events visible."""
        start_dt = _local_midnight(self._start)
        end_dt = _local_midnight(self._start + timedelta(days=self._day_count))
        earliest: int | None = None
        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("WeekView: failed to query instances for autoscroll")
            return
        for inst in instances:
            if inst.all_day:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            if not (
                self._start <= t.date() < self._start + timedelta(days=self._day_count)
            ):
                continue
            m = t.hour * 60 + t.minute
            if earliest is None or m < earliest:
                earliest = m
        target_minutes = earliest if earliest is not None else WORK_START_HOUR * 60
        # Scrollbar value is measured in scene Y. The viewport top sits at
        # scene Y = scrollbar.value(). The sticky header pins the first
        # header_h pixels of the viewport, so the first body row visible is
        # at scene Y = scrollbar.value() + header_h. Placing the target event
        # one body_top below scrollbar.value() puts it just under the header.
        target_y = target_minutes * self._px_per_hour / 60
        sb = self.verticalScrollBar()
        sb.setValue(max(0, min(sb.maximum(), int(target_y - 8))))

    # ── Wheel zoom (Ctrl+scroll) ─────────────────────────────────────────

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

    # ── Navigation ───────────────────────────────────────────────────────

    def navigate(self, weeks: int) -> None:
        self._start = self._start + timedelta(weeks=weeks)
        self._rebuild_grid()
        self.refresh()
        QTimer.singleShot(0, self._scroll_to_first_event)

    def go_today(self) -> None:
        today = date.today()
        self._start = today - timedelta(days=today.weekday())
        self._rebuild_grid()
        self.refresh()
        QTimer.singleShot(0, self._scroll_to_first_event)

    def refresh_theme(self) -> None:
        self._scene.update()
        self.viewport().update()

    def go_to_date(self, d: date) -> None:
        self._start = d - timedelta(days=d.weekday())
        self._rebuild_grid()
        self.refresh()
        QTimer.singleShot(0, self._scroll_to_first_event)

    def range_label(self) -> str:
        end = self._start + timedelta(days=self._day_count - 1)
        if self._start.month == end.month:
            return f"{self._start.strftime('%B %-d')}–{end.strftime('%-d, %Y')}"
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self) -> None:
        # _reposition_chips() already clears its own state — just call it.
        self._reposition_chips()

    # ── Chip placement ───────────────────────────────────────────────────

    def _reposition_chips(self) -> None:
        # Clear any previously-placed chips/markers; this method is called from
        # both refresh() and resizeEvent(), and must be idempotent. Chips that
        # were parented to the sticky header don't show up in scene.items(),
        # so we walk both top-level scene items and sticky-header children.
        for chip in self._chips:
            parent = chip.parentItem()
            if parent is not None:  # type: ignore[reportUnnecessaryComparison]
                chip.setParentItem(None)  # type: ignore[reportArgumentType]
            if chip.scene() is self._scene:
                self._scene.removeItem(chip)
        self._chips.clear()
        for child in list(self._sticky.childItems()):
            if isinstance(child, _MoreMarker):
                child.setParentItem(None)  # type: ignore[reportArgumentType]
                if child.scene() is self._scene:
                    self._scene.removeItem(child)
        for item in list(self._scene.items()):
            if isinstance(item, _MoreMarker):
                self._scene.removeItem(item)

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

        events = self._store.events_for_instances(instances)
        cal_color: dict[str, str | None] = {
            cal.id: cal.color
            for acc in self._store.list_accounts()
            for cal in self._store.list_calendars(acc.id, visible_only=False)
        }
        # First pass: count all-day-per-day to size the all-day band.
        all_day_rows_per_col = [0] * self._day_count
        for inst in instances:
            if not inst.all_day:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            day_offset = (t.date() - self._start).days
            if 0 <= day_offset < self._day_count:
                all_day_rows_per_col[day_offset] += 1
        max_rows = max(all_day_rows_per_col, default=0)
        if max_rows == 0:
            band_h = ALL_DAY_BAND_MIN
        else:
            rows_shown = min(max_rows, ALL_DAY_MAX_ROWS)
            band_h = 4 + rows_shown * ALL_DAY_ROW_H
        if abs(band_h - self._grid.all_day_band_h) > 0.5:
            self._grid.set_all_day_band_h(band_h)
            self._sticky.set_all_day_band_h(band_h)
            self._scene.setSceneRect(self._grid.boundingRect())
        else:
            # Keep sticky header band height in sync even when grid didn't change.
            self._sticky.set_all_day_band_h(band_h)

        body_top = self._grid.hour_top()
        per_col_all_day_idx = [0] * self._day_count
        # Timed events bucketed per day_offset; we lay them out after this
        # pass via the cascade packer so that overlaps render side-by-side.
        timed_by_day: list[list[tuple[float, float, dict[str, object]]]] = [
            [] for _ in range(self._day_count)
        ]

        for inst in instances:
            event = events.get(id(inst))
            if event is None:
                continue
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            day_offset = (t.date() - self._start).days
            if day_offset < 0 or day_offset >= self._day_count:
                continue

            if inst.all_day:
                row = per_col_all_day_idx[day_offset]
                per_col_all_day_idx[day_offset] += 1
                if row >= ALL_DAY_MAX_ROWS:
                    continue
                x = TIME_AXIS_WIDTH + day_offset * col_w
                y = DAY_HEADER_H + 2 + row * ALL_DAY_ROW_H
                h = ALL_DAY_ROW_H - 2
                chip = EventChip(
                    event,
                    QRectF(x + 1, y, col_w - 2, h),
                    calendar_color=cal_color[inst.calendar_id],
                    mode=self._chip_mode,
                    show_time_prefix=False,
                    instance_dtstart=t,
                )
                self._wire_chip_signals(chip)
                chip.setParentItem(self._sticky)
                self._chips.append(chip)
                continue

            # Timed: bucket it for cascade layout below.
            try:
                end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
            except (ValueError, TypeError):
                end_t = t
            start_min = t.hour * 60 + t.minute
            end_min = end_t.hour * 60 + end_t.minute
            # Force at least a 15-min visual extent so back-to-back zero-
            # length events still get laid out and aren't treated as point
            # events when packing.
            if end_min <= start_min:
                end_min = start_min + 15
            timed_by_day[day_offset].append(
                (
                    float(start_min),
                    float(end_min),
                    {
                        "event": event,
                        "inst": inst,
                        "start_dt": t,
                        "cal_color": cal_color[inst.calendar_id],
                        "instance_dtstart": t,
                    },
                )
            )

        # Cascade-pack and emit chips per day column.
        for day_offset, bucket in enumerate(timed_by_day):
            if not bucket:
                continue
            packed = pack_overlapping(bucket)
            day_x = TIME_AXIS_WIDTH + day_offset * col_w
            for (col_i, cols, xspan, payload), (start_min, end_min, _) in zip(
                packed, bucket, strict=True
            ):
                sub_w = (col_w - 2) / cols
                chip_x = day_x + 1 + col_i * sub_w
                chip_w = max(8.0, xspan * sub_w)
                chip_y = body_top + start_min * self._px_per_hour / 60
                chip_h = max(14.0, (end_min - start_min) * self._px_per_hour / 60)
                _tfmt = "%-I:%M %p" if self._time_format == "12h" else "%H:%M"
                chip = EventChip(
                    payload["event"],
                    QRectF(chip_x, chip_y, chip_w, chip_h),
                    calendar_color=payload["cal_color"],
                    mode=self._chip_mode,
                    time_prefix=payload["start_dt"].strftime(_tfmt),
                    time_format=self._time_format,
                    show_time_prefix=True,
                    overlap_cols=cols,
                    instance_dtstart=payload.get("instance_dtstart"),
                )
                self._wire_chip_signals(chip)
                self._scene.addItem(chip)
                self._chips.append(chip)

        # All-day overflow marker per column. Parented to the sticky header
        # so it pins with the all-day chips.
        for col, count in enumerate(all_day_rows_per_col):
            hidden = count - ALL_DAY_MAX_ROWS
            if hidden <= 0:
                continue
            x = TIME_AXIS_WIDTH + col * col_w
            y = DAY_HEADER_H + 2 + (ALL_DAY_MAX_ROWS - 1) * ALL_DAY_ROW_H
            marker = _MoreMarker(
                QRectF(x + 1, y, col_w - 2, ALL_DAY_ROW_H - 2),
                f"+{hidden} more",
            )
            marker.setParentItem(self._sticky)

    def _on_details_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_details_dialog

        open_details_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    def _on_edit_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_edit_dialog

        open_edit_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    def _on_delete_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_delete_dialog

        open_delete_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    # ── Chip signal wiring ────────────────────────────────────────────────

    def _wire_chip_signals(self, chip: "EventChip") -> None:
        chip.details_requested.connect(
            lambda ev, c=chip: self._on_details_requested(ev, c.instance_dtstart)
        )
        chip.edit_requested.connect(
            lambda ev, c=chip: self._on_edit_requested(ev, c.instance_dtstart)
        )
        chip.delete_requested.connect(
            lambda ev, c=chip: self._on_delete_requested(ev, c.instance_dtstart)
        )
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

    def _scene_x_to_day_offset(self, scene_x: float) -> int | None:
        col_w = max(
            1.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )
        offset = int((scene_x - TIME_AXIS_WIDTH) / col_w)
        if offset < 0 or offset >= self._day_count:
            return None
        return offset

    def _compute_timed_chip_rect(
        self, day_off: int, start_min: int, end_min: int
    ) -> QRectF:
        col_w = max(
            1.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )
        body_top = self._grid.hour_top()
        pph = self._px_per_hour
        x = TIME_AXIS_WIDTH + day_off * col_w + 1
        y = body_top + start_min * pph / 60
        w = col_w - 2
        h = max(14.0, (end_min - start_min) * pph / 60)
        return QRectF(x, y, w, h)

    def _compute_allday_chip_rect(self, start_off: int, end_off: int) -> QRectF:
        col_w = max(
            1.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )
        scroll_y = float(self.verticalScrollBar().value())
        x = TIME_AXIS_WIDTH + start_off * col_w + 1
        y = scroll_y + DAY_HEADER_H + 2
        w = (end_off - start_off + 1) * col_w - 2
        return QRectF(x, y, w, ALL_DAY_ROW_H - 2)

    def _format_drag_label(
        self, mode: str, day_off: int, start_min: int, end_min: int
    ) -> str:
        sh, sm = divmod(start_min, 60)
        eh, em = divmod(end_min % 1440, 60)
        duration = end_min - start_min
        dh, dm = divmod(duration, 60)
        time_str = f"{sh:02d}:{sm:02d} – {eh:02d}:{em:02d}"
        if dh and dm:
            dur_str = f"({dh}h {dm}m)"
        elif dh:
            dur_str = f"({dh}h)"
        else:
            dur_str = f"({dm}m)"
        if mode == "move":
            day_name = (self._start + timedelta(days=day_off)).strftime("%a")
            return f"{day_name}  {time_str}"
        return f"{time_str}  {dur_str}"

    # ── View-level mouse overrides (create-flow) ──────────────────────────

    @override
    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        vp_pos = event.pos()
        vp_y = vp_pos.y()

        # Ignore clicks on the day-label column-header row.
        if vp_y < DAY_HEADER_H:
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(vp_pos)

        # If the click landed on a chip, let Qt dispatch it to the chip.
        item = self._scene.itemAt(scene_pos, self.viewportTransform())
        if isinstance(item, EventChip):
            super().mousePressEvent(event)
            return

        # Must be within a valid day column (not in the time-axis gutter).
        day_off = self._scene_x_to_day_offset(scene_pos.x())
        if day_off is None:
            super().mousePressEvent(event)
            return

        header_h = self._sticky.header_h()

        if vp_y < header_h:
            # All-day band
            self._drag_kind = "create_allday"
            self._drag_day_offset = day_off
            self._drag_end_day_offset = day_off
            self._press_scene_pos = scene_pos
        else:
            # Timed body
            start_min = self._snap_minutes_to(self._scene_y_to_minutes(scene_pos.y()))
            self._drag_kind = "create_body"
            self._drag_day_offset = day_off
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
            current_day = self._scene_x_to_day_offset(scene_pos.x())
            if current_day is None:
                current_day = self._drag_day_offset
            self._drag_current_min = current_min
            start_min = min(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            end_min = max(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            if end_min <= start_min:
                end_min = start_min + self._snap_minutes
            rect = self._compute_timed_chip_rect(
                self._drag_day_offset,  # type: ignore[reportArgumentType]
                start_min,
                end_min,  # type: ignore[reportArgumentType]
            )
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
            end_day = self._scene_x_to_day_offset(scene_pos.x())
            if end_day is None:
                end_day = (
                    self._drag_end_day_offset
                    if self._drag_end_day_offset is not None
                    else self._drag_day_offset
                )
            self._drag_end_day_offset = end_day
            start_off = min(self._drag_day_offset, end_day)  # type: ignore[reportArgumentType]
            end_off = max(self._drag_day_offset, end_day)  # type: ignore[reportArgumentType]
            rect = self._compute_allday_chip_rect(start_off, end_off)
            n_days = end_off - start_off + 1
            label = f"All day · {n_days} day{'s' if n_days > 1 else ''}"

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
            start_min = min(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            end_min = max(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            if end_min - start_min < self._snap_minutes:
                end_min = start_min + 60
            day_date = self._start + timedelta(days=self._drag_day_offset)  # type: ignore[reportArgumentType]
            tz = local_zoneinfo()
            start_dt = datetime(
                day_date.year,
                day_date.month,
                day_date.day,
                start_min // 60,
                start_min % 60,
                tzinfo=tz,
            )
            end_dt = start_dt + timedelta(minutes=end_min - start_min)
            self._teardown_preview()
            self._drag_kind = None
            self._open_create_dialog(start_dt, end_dt, all_day=False)
        else:  # create_allday
            start_off = self._drag_day_offset
            end_off = (
                self._drag_end_day_offset
                if self._drag_end_day_offset is not None
                else start_off
            )
            start_off, end_off = min(start_off, end_off), max(start_off, end_off)  # type: ignore[reportArgumentType]
            start_date = self._start + timedelta(days=start_off)
            end_date = self._start + timedelta(days=end_off)
            tz = local_zoneinfo()
            start_dt = datetime(
                start_date.year, start_date.month, start_date.day, tzinfo=tz
            )
            end_day_excl = end_date + timedelta(days=1)
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
                self._drag_day_offset = None
                self._drag_end_day_offset = None
                self._drag_current_min = None
                self._press_scene_pos = None
                event.accept()
                return
            if self._drag_chip_event is not None:
                for chip in self._chips:
                    if chip._event is self._drag_chip_event:  # type: ignore[reportPrivateUsage]
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
        col_w = max(
            1.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )
        body_top = self._grid.hour_top()

        if self._drag_chip_event is None:
            self._drag_chip_event = event
            self._drag_chip_mode = mode
            for chip in self._chips:
                if chip._event is event:  # type: ignore[reportPrivateUsage]
                    r = chip.sceneBoundingRect()
                    self._press_scene_pos = chip._press_scene_pos  # type: ignore[reportPrivateUsage]
                    origin_day = int((r.left() - TIME_AXIS_WIDTH) / col_w)
                    if event.all_day:
                        self._drag_chip_origin = (origin_day, 0, 0)
                    else:
                        origin_start = int((r.top() - body_top) * 60 / pph)
                        origin_end = int((r.bottom() - body_top) * 60 / pph)
                        self._drag_chip_origin = (origin_day, origin_start, origin_end)
                    break
            else:
                return

        if self._drag_chip_origin is None:
            return
        origin_day, origin_start, origin_end = self._drag_chip_origin
        duration = origin_end - origin_start

        if event.all_day:
            if self._press_scene_pos is None:
                return
            dx = scene_pos.x() - self._press_scene_pos.x()
            delta_day = int(round(dx / col_w))
            new_day = max(0, min(self._day_count - 1, origin_day + delta_day))
            if event.dtstart and event.dtend:
                span = max(1, (event.dtend.date() - event.dtstart.date()).days)
            else:
                span = 1
            end_day = min(self._day_count - 1, new_day + span - 1)
            rect = self._compute_allday_chip_rect(new_day, end_day)
            day_name = (self._start + timedelta(days=new_day)).strftime("%a")
            label = f"→ {day_name}"
            if self._drag_preview is None:
                self._drag_preview = DragPreview(rect, label)
                self._scene.addItem(self._drag_preview)
            else:
                self._drag_preview.set_rect(rect)
                self._drag_preview.set_label(label)
            return

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = max(0, min(1440 - duration, new_start))
            new_end = new_start + duration
            new_day_x = self._scene_x_to_day_offset(scene_pos.x())
            new_day = new_day_x if new_day_x is not None else origin_day
        elif mode == "resize_top":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = min(new_start, origin_end - self._snap_minutes)
            new_end = origin_end
            new_day = origin_day
        else:  # resize_bottom
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_end = self._snap_minutes_to(cursor_min)
            new_end = max(new_end, origin_start + self._snap_minutes)
            new_end = min(new_end, 1440)
            new_start = origin_start
            new_day = origin_day

        rect = self._compute_timed_chip_rect(new_day, new_start, new_end)
        label = self._format_drag_label(mode, new_day, new_start, new_end)

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

        col_w = max(
            1.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count
        )
        origin_day, origin_start, origin_end = self._drag_chip_origin
        duration = origin_end - origin_start

        if event.all_day:
            if self._press_scene_pos is None:
                return
            dx = scene_pos.x() - self._press_scene_pos.x()
            delta_day = int(round(dx / col_w))
            new_day = max(0, min(self._day_count - 1, origin_day + delta_day))
            if event.dtstart:
                new_dtstart = event.dtstart + timedelta(days=new_day - origin_day)
                new_dtend = (
                    (event.dtend + timedelta(days=new_day - origin_day))
                    if event.dtend
                    else (new_dtstart + timedelta(days=1))
                )
            else:
                new_day_date = self._start + timedelta(days=new_day)
                tz_local = local_zoneinfo()
                new_dtstart = datetime(
                    new_day_date.year,
                    new_day_date.month,
                    new_day_date.day,
                    tzinfo=tz_local,
                )
                new_dtend = new_dtstart + timedelta(days=1)
            updated = dataclasses.replace(
                event,
                dtstart=new_dtstart,
                dtend=new_dtend,
                sequence=event.sequence + 1,
                local_dirty=True,
            )
            self._store.queue_update(updated, event.etag)
            self._teardown_preview()
            self._drag_chip_event = None
            self._drag_chip_mode = None
            self._drag_chip_origin = None
            self._press_scene_pos = None
            return

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = max(0, min(1440 - duration, new_start))
            new_end = new_start + duration
            new_day_x = self._scene_x_to_day_offset(scene_pos.x())
            new_day = new_day_x if new_day_x is not None else origin_day
        elif mode == "resize_top":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_start = self._snap_minutes_to(cursor_min)
            new_start = min(new_start, origin_end - self._snap_minutes)
            new_end = origin_end
            new_day = origin_day
        else:  # resize_bottom
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            new_end = self._snap_minutes_to(cursor_min)
            new_end = max(new_end, origin_start + self._snap_minutes)
            new_end = min(new_end, 1440)
            new_start = origin_start
            new_day = origin_day

        new_day_date = self._start + timedelta(days=new_day)
        local_tz = local_zoneinfo()
        new_dtstart = datetime(
            new_day_date.year,
            new_day_date.month,
            new_day_date.day,
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
            self.parent(),  # type: ignore[reportArgumentType]
            store=self._store,
            default_dt=start_dt,
            default_dtend=end_dt,
            default_all_day=all_day,
        )
        if dlg.exec():
            new_event = dlg.build_event(str(uuid.uuid4()))
            self._store.queue_create(new_event)


class _MoreMarker(QGraphicsItem):
    """Tiny "+N more" pill drawn in the all-day overflow position."""

    def __init__(self, rect: QRectF, label: str) -> None:
        super().__init__()
        self._rect = rect
        self._label = label

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(theme.BG_SURFACE_3))
        painter.setPen(QPen(QColor(theme.BORDER), 0))
        painter.drawRoundedRect(self._rect, 2, 2)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX))
        painter.drawText(
            self._rect,
            Qt.AlignmentFlag.AlignCenter,
            self._label,
        )
