from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QSizePolicy,
)

from lilical.storage.event_store import EventStore
from lilical.ui import theme
from lilical.ui._time_fmt import fmt_hm
from lilical.ui.views._week_start import (
    dow_labels,
    start_of_week,
    weekend_columns,
)
from lilical.ui.widgets.day_events_popover import DayEventsPopover, PopoverEvent
from lilical.ui.widgets.event_chip import ChipMode, EventChip

log = logging.getLogger(__name__)

COLS = 7
ROWS = 6

_BASE_CELL_W = 140
_BASE_CELL_H = 100
_BASE_HEADER_H = 24
_BASE_PAD = 4
_BASE_CHIP_H = 16
_BASE_CHIP_GAP = 2
_BASE_MIN_CHIP_H = 4
_BASE_TODAY_RING_RADIUS = 11

CELL_W = _BASE_CELL_W
CELL_H = _BASE_CELL_H
HEADER_H = _BASE_HEADER_H
PAD = _BASE_PAD
CHIP_H = _BASE_CHIP_H
CHIP_GAP = _BASE_CHIP_GAP
MIN_CHIP_H = _BASE_MIN_CHIP_H
TODAY_RING_RADIUS = _BASE_TODAY_RING_RADIUS


def apply_scale(factor: float) -> None:
    g = globals()
    g["CELL_W"] = max(1, round(_BASE_CELL_W * factor))
    g["CELL_H"] = max(1, round(_BASE_CELL_H * factor))
    g["HEADER_H"] = max(1, round(_BASE_HEADER_H * factor))
    g["PAD"] = max(1, round(_BASE_PAD * factor))
    # Chip height must clear `min_title = title_fm.height() + 1` from
    # EventChip._tier_mins; otherwise the title is silently skipped (tier 0).
    title_fm = QFontMetricsF(QFont(theme.FONT_FAMILY, theme.FONT_CHIP_TITLE))
    g["CHIP_H"] = max(round(_BASE_CHIP_H * factor), math.ceil(title_fm.height()) + 2)
    g["CHIP_GAP"] = max(1, round(_BASE_CHIP_GAP * factor))
    g["MIN_CHIP_H"] = max(2, round(_BASE_MIN_CHIP_H * factor))
    g["TODAY_RING_RADIUS"] = max(1, round(_BASE_TODAY_RING_RADIUS * factor))


def _local_midnight(d: date) -> datetime:
    """Return midnight of `d` in the system local timezone as a UTC-aware datetime."""
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


class MonthGrid(QGraphicsItem):
    def __init__(self, year: int, month: int, week_start: str = "monday") -> None:
        super().__init__()
        self._year = year
        self._month = month
        self._week_start = week_start
        self._first = date(year, month, 1)
        if month == 12:
            self._last = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            self._last = date(year, month + 1, 1) - timedelta(days=1)
        self._start = start_of_week(self._first, week_start)
        self._today = date.today()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, COLS * CELL_W, HEADER_H + ROWS * CELL_H)

    @property
    def grid_start(self) -> date:
        return self._start

    @property
    def visible_month(self) -> tuple[int, int]:
        return self._year, self._month

    def cell_rect(self, day: date) -> QRectF | None:
        if day < self._start or day >= self._start + timedelta(days=42):
            return None
        offset = (day - self._start).days
        c = offset % 7
        r = offset // 7
        return QRectF(c * CELL_W, HEADER_H + r * CELL_H, CELL_W, CELL_H)

    def week_row_rect(self, row: int) -> QRectF:
        return QRectF(0, HEADER_H + row * CELL_H, COLS * CELL_W, CELL_H)

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Weekend column tint (Sat/Sun): always shade wherever they land.
        for c in weekend_columns(self._week_start):
            painter.fillRect(
                QRectF(c * CELL_W, HEADER_H, CELL_W, ROWS * CELL_H),
                QColor(theme.BG_WEEKEND),
            )

        # Day-of-week strip.
        days = dow_labels(self._week_start)
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_MONTH_HEADER, QFont.Weight.Bold)
        )
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        for i, d in enumerate(days):
            painter.drawText(
                QRectF(i * CELL_W, 0, CELL_W, HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                d,
            )

        # Grid lines.
        painter.setPen(QPen(QColor(theme.BORDER)))
        for r in range(ROWS + 1):
            y = HEADER_H + r * CELL_H
            painter.drawLine(0, y, COLS * CELL_W, y)
        for c in range(COLS + 1):
            x = c * CELL_W
            painter.drawLine(x, HEADER_H, x, HEADER_H + ROWS * CELL_H)

        # Header underline.
        painter.setPen(QPen(QColor(theme.BORDER_STRONG)))
        painter.drawLine(0, HEADER_H, COLS * CELL_W, HEADER_H)

        # Day numbers.
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_DAY_NUMBER, QFont.Weight.Bold)
        )
        cur = self._start
        for r in range(ROWS):
            for c in range(COLS):
                x = c * CELL_W + PAD
                y = HEADER_H + r * CELL_H + PAD
                in_month = self._first <= cur <= self._last
                is_today = cur == self._today and in_month

                # Today: hollow ring around the number, not a fill.
                if is_today:
                    ring_cx = x + TODAY_RING_RADIUS - 2
                    ring_cy = y + TODAY_RING_RADIUS - 2
                    painter.setPen(QPen(QColor(theme.ACCENT), 1.5))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(
                        QRectF(
                            ring_cx - TODAY_RING_RADIUS,
                            ring_cy - TODAY_RING_RADIUS,
                            TODAY_RING_RADIUS * 2,
                            TODAY_RING_RADIUS * 2,
                        )
                    )

                painter.setPen(
                    QColor(theme.TEXT_PRIMARY)
                    if in_month
                    else QColor(theme.TEXT_DISABLED)
                )
                painter.drawText(x + 2, y + 14, str(cur.day))
                cur += timedelta(days=1)


def _query_month_data(
    store, grid_start: date, end_day: date, cal_info_snap: dict
) -> dict | None:
    """Off-thread: query DB for the month range."""
    start_dt = _local_midnight(grid_start)
    end_dt = _local_midnight(end_day)
    visible_ids = {ci.id for ci in cal_info_snap.values() if ci.visible}
    try:
        instances = store.list_instances(start_dt, end_dt, calendar_ids=visible_ids)
    except Exception:
        log.exception("MonthView: failed to query instances")
        return None
    events = store.events_for_instances(instances)
    completions = store.completion_for_instances(instances)
    cal_color: dict[str, str | None] = {
        ci.id: ci.color for ci in cal_info_snap.values()
    }
    return {
        "instances": instances,
        "events": events,
        "cal_color": cal_color,
        "grid_start": grid_start,
        "completions": completions,
    }


class MonthView(QGraphicsView):
    day_activated = Signal(object)  # emits date — for switching to Day view
    new_event_requested = Signal(object)  # emits date — double-click to create

    def __init__(self, store: EventStore, cal_info_provider=None) -> None:
        super().__init__()
        self._store = store
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._cached_data: dict | None = None
        self._chip_mode: ChipMode = ChipMode.BARS
        self._chips: list[QGraphicsItem] = []
        self._event_chips: dict[tuple, EventChip] = {}
        self._refresh_task: asyncio.Task | None = None
        self._rendered_month: tuple[int, int] | None = None

        # Hover-popover state
        self._cell_dense: set[date] = set()
        self._cell_popover_events: dict[date, list[PopoverEvent]] = {}
        self._hovered_day: date | None = None
        self._pending_day: date | None = None
        self._popover = DayEventsPopover()
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_popover)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        now = date.today()
        self._year = now.year
        self._month = now.month
        self._week_start = "monday"
        self._time_format = "24h"
        self._completed_enabled = False
        self._grid = MonthGrid(now.year, now.month, self._week_start)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_month = (self._year, self._month)
        store.instance_completion_changed.connect(self._on_completion_changed)

    @override
    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh(data_dirty=False)

    @override
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._cached_data is None:
            self.refresh()

    @override
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        # If a scene item captured the double-click (a chip), let it handle it.
        scene_pos = self.mapToScene(event.position().toPoint())
        top = self._scene.itemAt(scene_pos, self.transform())
        if top is not None and top is not self._grid:
            super().mouseDoubleClickEvent(event)
            return
        # Map scene Y to a day cell (Y must be below the header strip).
        if scene_pos.y() < HEADER_H:
            super().mouseDoubleClickEvent(event)
            return
        col = int(scene_pos.x() // CELL_W)
        row = int((scene_pos.y() - HEADER_H) // CELL_H)
        if col < 0 or col >= COLS or row < 0 or row >= ROWS:
            super().mouseDoubleClickEvent(event)
            return
        offset = row * COLS + col
        d = self._grid.grid_start + timedelta(days=offset)
        self.new_event_requested.emit(d)
        event.accept()

    @override
    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        super().mouseMoveEvent(event)
        scene_pos = self.mapToScene(event.position().toPoint())
        day = self._day_at_scene(scene_pos)
        if day != self._hovered_day:
            self._hovered_day = day
            self._show_timer.stop()
            self._popover.hide()
            if day is not None and day in self._cell_dense:
                self._pending_day = day
                self._show_timer.start(280)

    @override
    def leaveEvent(self, event) -> None:  # noqa: ANN001
        super().leaveEvent(event)
        self._hovered_day = None
        self._pending_day = None
        self._show_timer.stop()
        self._popover.hide()

    def _day_at_scene(self, pos: QPointF) -> date | None:
        if pos.y() < HEADER_H:
            return None
        col = int(pos.x() // CELL_W)
        row = int((pos.y() - HEADER_H) // CELL_H)
        if col < 0 or col >= COLS or row < 0 or row >= ROWS:
            return None
        offset = row * COLS + col
        return self._grid.grid_start + timedelta(days=offset)

    def _show_popover(self) -> None:
        day = self._pending_day
        if day is None or day != self._hovered_day:
            return
        events = self._cell_popover_events.get(day)
        if not events:
            return
        cell_rect = self._grid.cell_rect(day)
        if cell_rect is None:
            return
        scene_pt = QPointF(cell_rect.right(), cell_rect.top())
        vp_pt = self.mapFromScene(scene_pt)
        global_pt: QPoint = self.viewport().mapToGlobal(vp_pt)
        self._popover.show_for_day(day, events, global_pt)

    def _rebuild_grid(self) -> None:
        self._scene.removeItem(self._grid)
        self._grid = MonthGrid(self._year, self._month, self._week_start)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_month = (self._year, self._month)

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
        self.refresh()

    def go_today(self) -> None:
        today = date.today()
        self._year, self._month = today.year, today.month
        self.refresh()

    def refresh_theme(self) -> None:
        self._scene.update()
        self.viewport().update()

    def go_to_date(self, d: date) -> None:
        self._year, self._month = d.year, d.month
        self.refresh()

    def range_label(self) -> str:
        return date(self._year, self._month, 1).strftime("%B %Y")

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
        self.refresh(data_dirty=False)

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def set_week_start(self, week_start: str) -> None:
        if week_start == self._week_start:
            return
        self._week_start = week_start
        self._rebuild_grid()
        self.refresh()

    def set_time_format(self, fmt: str) -> None:
        if fmt == self._time_format:
            return
        self._time_format = fmt
        self.refresh(data_dirty=False)

    def set_completed_events_enabled(self, enabled: bool) -> None:
        if enabled == self._completed_enabled:
            return
        self._completed_enabled = enabled
        for chip in self._event_chips.values():
            chip.set_completed_display(enabled)
        self.viewport().update()

    def _on_completion_changed(
        self, _cal_id: str, _uid: str, _dtstart_utc: int
    ) -> None:
        self.refresh()

    def refresh(self, *, data_dirty: bool = True) -> None:
        if not data_dirty and self._cached_data is not None:
            self._apply_plan(self._cached_data)
            return
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        first = date(self._year, self._month, 1)
        grid_start = start_of_week(first, self._week_start)
        end_day = grid_start + timedelta(days=42)
        cal_info_snap = self._cal_info_provider()
        self._refresh_task = asyncio.ensure_future(
            self._refresh_async(grid_start, end_day, cal_info_snap)
        )

    async def _refresh_async(
        self, grid_start: date, end_day: date, cal_info_snap: dict
    ) -> None:
        try:
            data = await asyncio.to_thread(
                _query_month_data, self._store, grid_start, end_day, cal_info_snap
            )
        except asyncio.CancelledError:
            return
        if data is None:
            for chip in self._event_chips.values():
                self._scene.removeItem(chip)
            self._event_chips = {}
            return
        self._cached_data = data
        self._apply_plan(data)

    def _apply_plan(self, plan: dict) -> None:
        if self._rendered_month != (self._year, self._month):
            self._rebuild_grid()
        instances = plan["instances"]
        events = plan["events"]
        cal_color: dict[str, str | None] = plan["cal_color"]
        grid_start: date = plan["grid_start"]
        completions: frozenset = plan.get("completions", frozenset())

        # Remove non-EventChip overlay items (rebuilt every refresh).
        for item in self._chips:
            if not isinstance(item, EventChip):
                self._scene.removeItem(item)
        self._chips.clear()

        # Diff state for EventChip reuse.
        old_event_chips = self._event_chips
        new_event_chips: dict[tuple, EventChip] = {}

        # Per-cell popover data (rebuilt each refresh).
        cell_popover: dict[date, list[PopoverEvent]] = {}
        cell_dense: set[date] = set()

        # Build per-day buckets, distinguishing multi-day from single-day.
        single_by_day: dict[date, list[tuple[datetime, object]]] = {}
        multi_spans: list[tuple[date, date, object]] = []
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
                et = datetime.fromisoformat(inst.dtend_local).astimezone()
            except (ValueError, TypeError):
                continue

            start_day = t.date()
            end_day_inclusive = et.date()
            ends_at_midnight = et.time().hour == 0 and et.time().minute == 0
            if ends_at_midnight and end_day_inclusive > start_day:
                end_day_inclusive = end_day_inclusive - timedelta(days=1)

            if end_day_inclusive > start_day:
                multi_spans.append((start_day, end_day_inclusive, inst))
            else:
                single_by_day.setdefault(start_day, []).append((t, inst))

        # ── Layout pass 1: place multi-day spans in row slots ────────────
        row_slots_used: dict[int, list[list[tuple[int, int]]]] = {}

        def _row_for_date(d: date) -> int | None:  # noqa: ARG001  # type: ignore[reportUnusedFunction]
            offset = (d - grid_start).days
            if offset < 0 or offset >= 42:
                return None
            return offset // 7

        def cell_row_col(d: date) -> tuple[int, int] | None:
            offset = (d - grid_start).days
            if offset < 0 or offset >= 42:
                return None
            return offset // 7, offset % 7

        max_chips_per_cell = max(1, int((CELL_H - 22) / (CHIP_H + CHIP_GAP)))

        # Track which multi-day track is the highest occupied per cell, so
        # single-day events can stack below them.
        per_cell_max_multi_track: dict[tuple[int, int], int] = {}

        multi_spans.sort(key=lambda x: ((x[1] - x[0]).days, x[0]), reverse=True)

        for s_day, e_day, inst in multi_spans:
            event = events.get(id(inst))
            if event is None:
                continue
            try:
                inst_t = datetime.fromisoformat(inst.dtstart_local).astimezone()  # type: ignore[reportAttributeAccessIssue]
            except (ValueError, TypeError):
                inst_t = None
            visible_start = max(s_day, grid_start)
            visible_end = min(e_day, grid_start + timedelta(days=41))
            if visible_end < visible_start:
                continue

            d = visible_start
            while d <= visible_end:
                rc = cell_row_col(d)
                if rc is None:
                    d += timedelta(days=1)
                    continue
                row, col = rc
                row_end_day = grid_start + timedelta(days=row * 7 + 6)
                seg_end = min(visible_end, row_end_day)
                _seg_rc = cell_row_col(seg_end)
                if _seg_rc is None:
                    d = seg_end + timedelta(days=1)
                    continue
                seg_end_col = _seg_rc[1]

                # Find a free track.
                tracks = row_slots_used.setdefault(row, [])
                placed_track = None
                for t_idx, spans in enumerate(tracks):
                    if all(end < col or start > seg_end_col for start, end in spans):
                        placed_track = t_idx
                        break
                if placed_track is None:
                    placed_track = len(tracks)
                    tracks.append([])
                tracks[placed_track].append((col, seg_end_col))

                if placed_track >= max_chips_per_cell:
                    # No room: add to popover for every covered cell.
                    time_str = "All day"
                    pev = PopoverEvent(
                        time_str=time_str,
                        title=event.summary or "",
                        location=event.location,
                        calendar_color=cal_color.get(inst.calendar_id),  # type: ignore[reportAttributeAccessIssue]
                    )
                    for cc in range(col, seg_end_col + 1):
                        day_key = grid_start + timedelta(days=row * 7 + cc)
                        cell_popover.setdefault(day_key, []).append(pev)
                        cell_dense.add(day_key)
                else:
                    cell = self._grid.cell_rect(d)
                    if cell is None:
                        d = seg_end + timedelta(days=1)
                        continue
                    x = col * CELL_W + 2
                    w = (seg_end_col - col + 1) * CELL_W - 4
                    y = (
                        HEADER_H
                        + row * CELL_H
                        + 22
                        + placed_track * (CHIP_H + CHIP_GAP)
                    )
                    _chip_key = (
                        inst.calendar_id,
                        inst.uid,
                        inst.dtstart_local,
                        "multi",
                        row,
                        col,
                    )  # type: ignore[reportAttributeAccessIssue]
                    _is_comp = (
                        inst.calendar_id, inst.uid, inst.dtstart_utc  # type: ignore[reportAttributeAccessIssue]
                    ) in completions
                    self._place_event_chip(
                        _chip_key,
                        event,
                        QRectF(x, y, w, CHIP_H),
                        calendar_color=cal_color.get(inst.calendar_id),  # type: ignore[reportAttributeAccessIssue]
                        show_time_prefix=False,
                        time_prefix=None,
                        continues_left=(s_day < grid_start + timedelta(days=row * 7)),
                        continues_right=(e_day > row_end_day),
                        instance_dtstart=inst_t,
                        completed=_is_comp,
                        inst_key=(  # type: ignore[reportAttributeAccessIssue]
                            inst.calendar_id, inst.uid, inst.dtstart_utc
                        ),
                        old_chips=old_event_chips,
                        new_chips=new_event_chips,
                    )

                    # Track highest occupied multi-day track per cell.
                    for cc in range(col, seg_end_col + 1):
                        key_rc = (row, cc)
                        prev = per_cell_max_multi_track.get(key_rc, -1)
                        if placed_track > prev:
                            per_cell_max_multi_track[key_rc] = placed_track

                d = seg_end + timedelta(days=1)

        # ── Layout pass 2: single-day events fill remaining space ────────
        for day, items in single_by_day.items():
            rc = cell_row_col(day)
            if rc is None:
                continue
            row, col = rc

            # Start Y below all multi-day tracks in this cell.
            max_multi_track = per_cell_max_multi_track.get((row, col), -1)
            top_y_in_cell = 22 + (max_multi_track + 1) * (CHIP_H + CHIP_GAP)
            avail_h = max(0.0, float(CELL_H) - top_y_in_cell)

            items.sort(key=lambda x: (not x[1].all_day, x[0]))
            n_single = len(items)

            # Compute adaptive slot height.
            if n_single > 0 and avail_h > 0:
                gaps = max(0, n_single - 1) * CHIP_GAP
                slot_h_ideal = (avail_h - gaps) / n_single
                slot_h = max(float(MIN_CHIP_H), min(float(CHIP_H), slot_h_ideal))
                # How many fit at the minimum height?
                n_fit = min(
                    n_single,
                    int((avail_h + CHIP_GAP) / (MIN_CHIP_H + CHIP_GAP)),
                )
            else:
                slot_h = float(CHIP_H)
                n_fit = 0

            is_shrunk = slot_h < CHIP_H and n_fit > 0

            for i, (start_dt2, inst) in enumerate(items):
                event = events.get(id(inst))
                if event is None:
                    continue
                time_str_for_popover = (
                    "All day"
                    if inst.all_day
                    else fmt_hm(start_dt2.hour, start_dt2.minute, self._time_format)
                )
                pev = PopoverEvent(
                    time_str=time_str_for_popover,
                    title=event.summary or "",
                    location=event.location,
                    calendar_color=cal_color.get(inst.calendar_id),
                )
                cell_popover.setdefault(day, []).append(pev)

                if i >= n_fit:
                    # Truncated — mark dense but don't render chip.
                    cell_dense.add(day)
                    continue

                x = col * CELL_W + 2
                y = HEADER_H + row * CELL_H + top_y_in_cell + i * (slot_h + CHIP_GAP)
                time_prefix = None
                if not inst.all_day:
                    time_prefix = fmt_hm(
                        start_dt2.hour, start_dt2.minute, self._time_format
                    )
                _chip_key = (inst.calendar_id, inst.uid, inst.dtstart_local, "single")
                _is_comp = (inst.calendar_id, inst.uid, inst.dtstart_utc) in completions
                self._place_event_chip(
                    _chip_key,
                    event,
                    QRectF(x, y, CELL_W - 4, slot_h),
                    calendar_color=cal_color.get(inst.calendar_id),
                    show_time_prefix=not inst.all_day,
                    time_prefix=time_prefix,
                    instance_dtstart=start_dt2,
                    completed=_is_comp,
                    inst_key=(inst.calendar_id, inst.uid, inst.dtstart_utc),
                    old_chips=old_event_chips,
                    new_chips=new_event_chips,
                )

            if is_shrunk or n_fit < n_single:
                cell_dense.add(day)

        # Persist popover data.
        self._cell_dense = cell_dense
        self._cell_popover_events = cell_popover

        # Remove event chips that no longer appear in the layout.
        for key, chip in old_event_chips.items():
            if key not in new_event_chips:
                self._scene.removeItem(chip)
        self._event_chips = new_event_chips

    def _place_event_chip(
        self,
        key: tuple,
        event,
        rect: "QRectF",
        *,
        calendar_color,
        show_time_prefix: bool,
        time_prefix,
        continues_left: bool = False,
        continues_right: bool = False,
        instance_dtstart,
        completed: bool = False,
        inst_key: tuple[str, str, int] | None = None,
        old_chips: dict,
        new_chips: dict,
    ) -> EventChip:
        if key in old_chips:
            chip = old_chips[key]
            chip.update_event_data(event, completed=completed, inst_key=inst_key)
            chip.update_layout(
                rect,
                calendar_color=calendar_color,
                time_prefix=time_prefix,
                show_time_prefix=show_time_prefix,
                continues_left=continues_left,
                continues_right=continues_right,
                instance_dtstart=instance_dtstart,
            )
            if chip.scene() is not self._scene:
                self._scene.addItem(chip)
        else:
            read_only_cal_ids = {
                cid
                for cid, ci in self._cal_info_provider().items()
                if getattr(ci, "read_only", False)
            }
            chip = EventChip(
                event,
                rect,
                calendar_color=calendar_color,
                mode=self._chip_mode,
                show_time_prefix=show_time_prefix,
                time_prefix=time_prefix,
                continues_left=continues_left,
                continues_right=continues_right,
                instance_dtstart=instance_dtstart,
                completed=completed,
                inst_key=inst_key,
                read_only=event.calendar_id in read_only_cal_ids,
            )
            chip.details_requested.connect(
                lambda ev, c=chip: self._on_details_requested(ev, c.instance_dtstart)
            )
            chip.edit_requested.connect(
                lambda ev, c=chip: self._on_edit_requested(ev, c.instance_dtstart)
            )
            chip.delete_requested.connect(
                lambda ev, c=chip: self._on_delete_requested(ev, c.instance_dtstart)
            )
            chip.toggle_complete_requested.connect(self._on_toggle_complete_requested)
            self._scene.addItem(chip)
        chip.set_completed_display(self._completed_enabled)
        new_chips[key] = chip
        self._chips.append(chip)
        return chip

    def _on_toggle_complete_requested(
        self, inst_key: tuple[str, str, int], completed: bool
    ) -> None:
        cal_id, uid, dtstart_utc = inst_key
        self._store.set_completed(cal_id, uid, dtstart_utc, completed)

    def _cell_of(self, rect: QRectF) -> tuple[int, int]:
        col = int(rect.x() // CELL_W)
        row = int((rect.y() - HEADER_H) // CELL_H)
        return row, col

    def _on_details_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_details_dialog

        open_details_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    def _on_edit_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_edit_dialog

        open_edit_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    def _on_delete_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_delete_dialog

        open_delete_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]
