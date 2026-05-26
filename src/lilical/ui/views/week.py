from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore
from lilical.ui import theme
from lilical.ui._time_fmt import fmt_hm, fmt_hour_label
from lilical.ui.views._multi_day import multi_day_span
from lilical.ui.views._overlap import pack_overlapping_lanes
from lilical.ui.views._week_start import start_of_week
from lilical.ui.widgets._popover_rows import (
    PopoverEvent,
    cluster_events_to_popover_events,
)
from lilical.ui.widgets.day_events_popover import DayEventsPopover
from lilical.ui.widgets.drag_preview import DragPreview
from lilical.ui.widgets.event_chip import ChipMode, EventChip
from lilical.ui.widgets.inspector_pane import InspectorPane
from lilical.ui.widgets.line_cluster import LineCluster
from lilical.utils.timezone import local_iana_tz, local_zoneinfo

log = logging.getLogger(__name__)

HOURS = 24

_BASE_TIME_AXIS_WIDTH = 60
_BASE_DAY_HEADER_H = 32
_BASE_ALL_DAY_ROW_H = 22
_BASE_MIN_ALL_DAY_ROW_H = 6
_BASE_ALL_DAY_BAND_MIN = 28
_BASE_ALL_DAY_BAND_MAX = 4 * _BASE_ALL_DAY_ROW_H + 4  # 92 px — budget for ~4 full rows
_BASE_DEFAULT_PX_PER_HOUR = 48
_BASE_PX_PER_HOUR_MIN = 20
_BASE_PX_PER_HOUR_MAX = 96

TIME_AXIS_WIDTH = _BASE_TIME_AXIS_WIDTH
DAY_HEADER_H = _BASE_DAY_HEADER_H
ALL_DAY_ROW_H = _BASE_ALL_DAY_ROW_H
MIN_ALL_DAY_ROW_H = _BASE_MIN_ALL_DAY_ROW_H
ALL_DAY_BAND_MIN = _BASE_ALL_DAY_BAND_MIN
ALL_DAY_BAND_MAX = _BASE_ALL_DAY_BAND_MAX
DEFAULT_PX_PER_HOUR = _BASE_DEFAULT_PX_PER_HOUR
PX_PER_HOUR_MIN = _BASE_PX_PER_HOUR_MIN
PX_PER_HOUR_MAX = _BASE_PX_PER_HOUR_MAX


def apply_scale(factor: float) -> None:
    g = globals()
    g["TIME_AXIS_WIDTH"] = max(1, round(_BASE_TIME_AXIS_WIDTH * factor))
    g["DAY_HEADER_H"] = max(1, round(_BASE_DAY_HEADER_H * factor))
    g["ALL_DAY_ROW_H"] = max(1, round(_BASE_ALL_DAY_ROW_H * factor))
    g["MIN_ALL_DAY_ROW_H"] = max(2, round(_BASE_MIN_ALL_DAY_ROW_H * factor))
    g["ALL_DAY_BAND_MIN"] = max(1, round(_BASE_ALL_DAY_BAND_MIN * factor))
    g["ALL_DAY_BAND_MAX"] = max(1, round(_BASE_ALL_DAY_BAND_MAX * factor))
    g["DEFAULT_PX_PER_HOUR"] = max(1, round(_BASE_DEFAULT_PX_PER_HOUR * factor))
    g["PX_PER_HOUR_MIN"] = max(1, round(_BASE_PX_PER_HOUR_MIN * factor))
    g["PX_PER_HOUR_MAX"] = max(1, round(_BASE_PX_PER_HOUR_MAX * factor))

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
        self._time_format = "24h"
        self.setZValue(-10)

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

    def set_time_format(self, fmt: str) -> None:
        self._time_format = fmt
        self.update()

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
                fmt_hour_label(h, self._time_format),
            )


class _WeekNowLine(QGraphicsItem):
    """Current-time indicator for Week view, drawn above event chips (z=50)."""

    def __init__(self, grid: WeekGrid) -> None:
        super().__init__()
        self._grid = grid
        self._y = 0.0
        self._x_start = 0.0
        self._x_end = 0.0
        self.setZValue(50)

    def set_grid(self, grid: WeekGrid) -> None:
        self._grid = grid
        self.refresh()

    def refresh(self) -> None:
        today = date.today()
        is_today_visible = (
            self._grid._start
            <= today
            < self._grid._start + timedelta(days=self._grid._day_count)
        )
        self.prepareGeometryChange()
        if is_today_visible:
            now = datetime.now().astimezone()
            minutes = now.hour * 60 + now.minute
            col_w = (self._grid._width - TIME_AXIS_WIDTH) / self._grid._day_count
            col_index = (today - self._grid._start).days
            self._x_start = TIME_AXIS_WIDTH + col_index * col_w
            self._x_end = self._x_start + col_w
            self._y = self._grid.hour_top() + minutes * self._grid.px_per_hour / 60
            self.setVisible(True)
        else:
            self.setVisible(False)
        self.update()

    @override
    def boundingRect(self) -> QRectF:
        return QRectF(0, self._y - 5, self._grid._width, 10)

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        line_color = QColor(theme.DANGER)
        line_color.setAlphaF(0.65)
        painter.setPen(QPen(line_color, 2))
        painter.drawLine(
            int(self._x_start), int(self._y), int(self._x_end), int(self._y)
        )
        painter.setBrush(QColor(theme.DANGER))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(TIME_AXIS_WIDTH - 4, self._y - 4, 8, 8))


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


def _query_week_data(
    store, start: date, day_count: int, cal_info_snap: dict
) -> dict | None:
    """Off-thread: query DB for the week range."""
    start_dt = _local_midnight(start)
    end_dt = _local_midnight(start + timedelta(days=day_count))
    visible_ids = {ci.id for ci in cal_info_snap.values() if ci.visible}
    try:
        instances = store.list_instances(start_dt, end_dt, calendar_ids=visible_ids)
    except Exception:
        log.exception("WeekView: failed to query instances")
        return None

    events = store.events_for_instances(instances)
    completions = store.completion_for_instances(instances)
    cal_color: dict[str, str | None] = {
        ci.id: ci.color for ci in cal_info_snap.values()
    }
    read_only_cal_ids: set[str] = {
        ci.id for ci in cal_info_snap.values() if getattr(ci, "read_only", False)
    }
    return {
        "instances": instances,
        "events": events,
        "cal_color": cal_color,
        "read_only_cal_ids": read_only_cal_ids,
        "start": start,
        "day_count": day_count,
        "completions": completions,
    }


def _compute_week_placements(
    data: dict, px_per_hour: int, time_format: str, col_w: float
) -> dict:
    """Pure geometry: compute chip placement rects from raw week data."""
    instances = data["instances"]
    events = data["events"]
    cal_color = data["cal_color"]
    read_only_cal_ids: set[str] = data.get("read_only_cal_ids", set())
    start = data["start"]
    day_count = data["day_count"]
    completions: frozenset = data.get("completions", frozenset())

    week_end = start + timedelta(days=day_count - 1)
    first_event_minutes: int | None = None

    # Collect band items (all-day + multi-day timed) and single-day timed instances.
    # band_items: (start_col, end_col, inst, inst_t, span_or_None)
    #   span_or_None is the (s_day, e_day) from multi_day_span for multi-day items, None for all-day.  # noqa: E501
    band_items: list[tuple[int, int, object, datetime, tuple | None]] = []
    timed_instances: list[tuple[object, datetime]] = []

    for inst in instances:
        try:
            t = datetime.fromisoformat(inst.dtstart_local).astimezone()
        except (ValueError, TypeError):
            continue
        span = multi_day_span(inst)
        if span is not None:
            s_day, e_day = span
            vis_start = max(s_day, start)
            vis_end = min(e_day, week_end)
            if vis_end < vis_start:
                continue
            start_col = (vis_start - start).days
            end_col = (vis_end - start).days
            band_items.append((start_col, end_col, inst, t, span))
        elif inst.all_day:
            day_offset = (t.date() - start).days
            if 0 <= day_offset < day_count:
                band_items.append((day_offset, day_offset, inst, t, None))
        else:
            day_offset = (t.date() - start).days
            if 0 <= day_offset < day_count:
                m = t.hour * 60 + t.minute
                if first_event_minutes is None or m < first_event_minutes:
                    first_event_minutes = m
                timed_instances.append((inst, t))
            elif day_offset == -1:
                # Event started yesterday; check for a short cross-midnight tail
                # that bleeds into day 0 of this week.
                try:
                    _end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
                except (ValueError, TypeError):
                    continue
                if _end_t.date() == start and (
                    _end_t.hour != 0 or _end_t.minute != 0
                ):
                    timed_instances.append((inst, t))

    # Greedy track assignment — longer spans get lower tracks so they don't overlap.
    order = sorted(
        range(len(band_items)), key=lambda i: -(band_items[i][1] - band_items[i][0])
    )
    item_track = [0] * len(band_items)
    track_spans: list[list[tuple[int, int]]] = []
    for i in order:
        sc, ec = band_items[i][0], band_items[i][1]
        placed: int | None = None
        for t_idx, spans in enumerate(track_spans):
            if all(e < sc or s > ec for s, e in spans):
                placed = t_idx
                break
        if placed is None:
            placed = len(track_spans)
            track_spans.append([])
        track_spans[placed].append((sc, ec))
        item_track[i] = placed

    # Compute per-column row counts from track assignment.
    all_day_rows_per_col = [0] * day_count
    for i, (sc, ec, _inst, _t, _span) in enumerate(band_items):
        track_idx = item_track[i]
        for col in range(sc, ec + 1):
            all_day_rows_per_col[col] = max(all_day_rows_per_col[col], track_idx + 1)

    max_rows = max(all_day_rows_per_col, default=0)
    if max_rows == 0:
        band_h = float(ALL_DAY_BAND_MIN)
        band_row_h = float(ALL_DAY_ROW_H)
        rows_shown = 0
    else:
        available = float(ALL_DAY_BAND_MAX - 4)
        row_h_ideal = available / max_rows
        band_row_h = max(
            float(MIN_ALL_DAY_ROW_H), min(float(ALL_DAY_ROW_H), row_h_ideal)
        )
        rows_shown = min(max_rows, int(available / band_row_h))
        band_h = 4.0 + rows_shown * band_row_h
    body_top = DAY_HEADER_H + band_h

    # Popover data: one list of PopoverEvent per day-column.
    band_popover_events: dict[int, list[PopoverEvent]] = {}
    band_dense_cols: set[int] = set()

    new_placements: dict[tuple, dict] = {}

    # Render band items.
    for i, (start_col, end_col, inst, inst_t, span) in enumerate(band_items):
        track_idx = item_track[i]
        event = events.get(id(inst))
        # Collect popover data for every event regardless of visibility.
        pev = PopoverEvent(
            time_str="All day",
            title=event.summary if event else "",
            location=event.location if event else None,
            calendar_color=cal_color.get(inst.calendar_id),  # type: ignore[reportAttributeAccessIssue]
        )
        for col in range(start_col, end_col + 1):
            band_popover_events.setdefault(col, []).append(pev)
        if track_idx >= rows_shown:
            continue
        if event is None:
            continue
        if span:
            s_day, e_day = span
            continues_left = s_day < start
            continues_right = e_day > week_end
            key = (
                inst.calendar_id,
                inst.uid,
                inst.dtstart_local,
                "band",
                start.isoformat(),
            )
        else:
            continues_left = continues_right = False
            key = (inst.calendar_id, inst.uid, inst.dtstart_local)
        x = TIME_AXIS_WIDTH + start_col * col_w
        y = DAY_HEADER_H + 2 + track_idx * band_row_h
        w = (end_col - start_col + 1) * col_w - 2
        h = band_row_h - 2
        new_placements[key] = {
            "rect": QRectF(x + 1, y, w, h),
            "calendar_color": cal_color.get(inst.calendar_id),
            "show_time_prefix": False,
            "time_prefix": None,
            "continues_left": continues_left,
            "continues_right": continues_right,
            "overlap_cols": 1,
            "instance_dtstart": inst_t,
            "is_sticky": True,
            "event": event,
            "inst_key": (inst.calendar_id, inst.uid, inst.dtstart_utc),
        }

    # Render timed events (single-day or cross-midnight short splits).
    timed_by_day: list[list[tuple]] = [[] for _ in range(day_count)]
    for inst, t in timed_instances:
        event = events.get(id(inst))
        if event is None:
            continue
        try:
            end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
        except (ValueError, TypeError):
            end_t = t
        start_day = t.date()
        end_day = end_t.date()
        # True cross-midnight: end is a later date AND not exactly 00:00
        # (midnight-ending events are single-day chips per half-open convention).
        crosses_midnight = end_day > start_day and (
            end_t.hour != 0 or end_t.minute != 0
        )
        start_offset = (start_day - start).days
        start_min = t.hour * 60 + t.minute

        # Build per-day segments: (day_off, s_min, e_min, c_left, c_right, show_pfx)
        segments: list[tuple[int, int, int, bool, bool, bool]] = []
        if not crosses_midnight:
            if end_day > start_day:
                # Midnight-ending: show full 8 PM → midnight on start day.
                e_min = 1440
            else:
                e_min = end_t.hour * 60 + end_t.minute
                if e_min <= start_min:
                    e_min = start_min + 15
            if 0 <= start_offset < day_count:
                segments.append((start_offset, start_min, e_min, False, False, True))
        else:
            # Day 1 (or tail only if start was before week boundary).
            if 0 <= start_offset < day_count:
                segments.append((start_offset, start_min, 1440, False, True, True))
            # Day 2 tail: 00:00 → end_min.
            end_offset = start_offset + 1
            e_min_day2 = end_t.hour * 60 + end_t.minute
            if 0 <= end_offset < day_count and e_min_day2 > 0:
                segments.append((end_offset, 0, e_min_day2, True, False, False))

        for day_off, s_min, e_min, c_left, c_right, show_pfx in segments:
            key = (inst.calendar_id, inst.uid, inst.dtstart_local, day_off)
            timed_by_day[day_off].append(
                (
                    float(s_min),
                    float(e_min),
                    inst.calendar_id,
                    {
                        "event": event,
                        "start_dt": t,
                        "cal_color": cal_color.get(inst.calendar_id),
                        "instance_dtstart": t,
                        "key": key,
                        "inst_key": (inst.calendar_id, inst.uid, inst.dtstart_utc),
                        "continues_left": c_left,
                        "continues_right": c_right,
                        "show_time_prefix": show_pfx,
                    },
                )
            )

    _tfmt = "%-I:%M %p" if time_format == "12h" else "%H:%M"
    is_own_fn = (
        (lambda c: c not in read_only_cal_ids)  # noqa: E501
        if read_only_cal_ids is not None
        else None
    )
    new_cluster_placements: dict = {}
    for day_offset, bucket in enumerate(timed_by_day):
        if not bucket:
            continue
        packed = pack_overlapping_lanes(bucket, col_w, is_own_calendar_fn=is_own_fn)
        day_x = TIME_AXIS_WIDTH + day_offset * col_w
        for x_off, chip_w, mode, payload_or_cluster in packed:
            if mode == "normal":
                payload = payload_or_cluster
                # end_min stored in bucket — find corresponding bucket entry
                # (match by key since pack preserves payload identity)
                bucket_entry = next(
                    (b for b in bucket if b[3] is payload), None
                )
                if bucket_entry is None:
                    continue
                start_min_f, end_min_f = bucket_entry[0], bucket_entry[1]
                chip_x = day_x + x_off
                chip_y = body_top + start_min_f * px_per_hour / 60
                chip_h = max(14.0, (end_min_f - start_min_f) * px_per_hour / 60)
                show_pfx = payload["show_time_prefix"]
                new_placements[payload["key"]] = {
                    "rect": QRectF(chip_x, chip_y, chip_w, chip_h),
                    "calendar_color": payload["cal_color"],
                    "show_time_prefix": show_pfx,
                    "time_prefix": payload["start_dt"].strftime(_tfmt) if show_pfx else None,  # noqa: E501
                    "continues_left": payload["continues_left"],
                    "continues_right": payload["continues_right"],
                    "overlap_cols": 1,
                    "instance_dtstart": payload["instance_dtstart"],
                    "is_sticky": False,
                    "event": payload["event"],
                    "inst_key": payload["inst_key"],
                }
            else:
                # Dense cluster: one entry covering all events.
                cluster_data = payload_or_cluster
                cluster_start_min = cluster_data["cluster_start_min"]
                cluster_end_min = cluster_data["cluster_end_min"]
                cluster_x = day_x + x_off
                cluster_y = body_top + cluster_start_min * px_per_hour / 60
                cluster_h = max(14.0, (cluster_end_min - cluster_start_min) * px_per_hour / 60)  # noqa: E501
                cluster_rect = QRectF(cluster_x, cluster_y, chip_w, cluster_h)
                cluster_key = tuple(
                    sorted(ev["payload"]["key"] for ev in cluster_data["events"])
                )
                new_cluster_placements[cluster_key] = {
                    "rect": cluster_rect,
                    "cluster_data": cluster_data,
                    "px_per_hour": px_per_hour,
                    "calendar_color_map": cal_color,
                    "time_format": time_format,
                    "read_only_cal_ids": read_only_cal_ids,
                    "day_offset": day_offset,
                }

    # Mark columns as dense when band shrank or any events were truncated.
    for col in range(day_count):
        if all_day_rows_per_col[col] > 0 and (
            band_row_h < ALL_DAY_ROW_H or all_day_rows_per_col[col] > rows_shown
        ):
            band_dense_cols.add(col)

    return {
        "new_placements": new_placements,
        "new_cluster_placements": new_cluster_placements,
        "band_h": float(band_h),
        "band_popover_events": band_popover_events,
        "band_dense_cols": band_dense_cols,
        "first_event_minutes": first_event_minutes,
        "completions": completions,
    }


class WeekView(QGraphicsView):
    day_header_activated = Signal(object)  # date — user clicked a day-column header

    def __init__(
        self,
        store: EventStore,
        day_count: int = 7,
        cal_info_provider=None,
        *,
        inspector: InspectorPane | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._cached_data: dict | None = None
        self._day_count = day_count if day_count in VALID_DAY_COUNTS else 7
        self._px_per_hour = DEFAULT_PX_PER_HOUR
        self._chip_mode: ChipMode = ChipMode.BARS
        self._time_format: str = "24h"
        self._week_start_pref: str = "monday"
        self._chips: dict[tuple, EventChip] = {}
        self._clusters: dict[tuple, LineCluster] = {}
        self._refresh_task: asyncio.Task | None = None
        # Hover-popover state for the all-day band.
        self._band_popover_events: dict[int, list[PopoverEvent]] = {}
        self._band_dense_cols: set[int] = set()
        self._current_band_h: float = float(ALL_DAY_BAND_MIN)
        self._hovered_band_col: int | None = None
        self._pending_band_col: int | None = None
        self._popover = DayEventsPopover()
        self._band_show_timer = QTimer(self)
        self._band_show_timer.setSingleShot(True)
        self._band_show_timer.timeout.connect(self._show_band_popover)
        # Right-side inspector pane (None in standalone tests).
        self._inspector = inspector
        self._rendered_start: date | None = None
        self._needs_scroll: bool = True
        self._completed_enabled: bool = False
        # Drag-to-create / move / resize state
        self._snap_minutes: int = 15
        # Active drag (either originated on empty grid or on a chip)
        self._drag_kind: str | None = None
        # "create_body" → drag-to-create timed event in body
        # "create_allday" → drag-to-create all-day event
        # "chip" → drag originated on a chip (move/resize)
        self._drag_day_offset: int | None = None
        self._drag_start_min: float | None = None
        self._drag_current_min: float | None = None
        self._drag_end_day_offset: int | None = None
        self._drag_chip_event = None  # the Event being dragged
        self._drag_chip_mode: str | None = (
            None  # "move" / "resize_top" / "resize_bottom"
        )
        # (day_off, start_min, end_min)
        self._drag_chip_origin: tuple[int, int, int] | None = None
        self._drag_chip_grab_offset_min: float | None = None
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
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        today = date.today()
        week_start = start_of_week(today, self._week_start_pref)
        self._start = week_start
        # Initial width is a temporary value — resizeEvent will set the real
        # viewport width as soon as the widget is laid out.
        self._grid = WeekGrid(
            week_start, self._day_count, 1, px_per_hour=self._px_per_hour
        )
        self._scene.addItem(self._grid)
        self._sticky = _StickyHeader(week_start, self._day_count, 1)
        self._scene.addItem(self._sticky)
        self._now_line = _WeekNowLine(self._grid)
        self._scene.addItem(self._now_line)
        self._now_line.refresh()
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_start = self._start

        self._now_timer = QTimer(self)
        self._now_timer.timeout.connect(self._now_line.refresh)
        self._now_timer.start(60_000)

        # Pin the sticky header to the viewport top as the user scrolls.
        self.verticalScrollBar().valueChanged.connect(self._on_v_scroll)

        store.instance_completion_changed.connect(self._on_completion_changed)

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
        self._now_line.set_grid(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_start = self._start

    @override
    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        w = self.viewport().width()
        self._grid.set_width(w)
        self._sticky.set_width(w)
        self._now_line.refresh()
        self._scene.setSceneRect(0, 0, w, self._grid.grid_height())
        self.refresh(data_dirty=False)

    @override
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._cached_data is None:
            self.refresh()

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
        self._needs_scroll = True
        self.refresh()

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
        self.refresh(data_dirty=False)

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def set_time_format(self, fmt: str) -> None:
        if fmt == self._time_format:
            return
        self._time_format = fmt
        self._grid.set_time_format(fmt)
        self.refresh(data_dirty=False)

    def set_completed_events_enabled(self, enabled: bool) -> None:
        if enabled == self._completed_enabled:
            return
        self._completed_enabled = enabled
        for chip in self._chips.values():
            chip.set_completed_display(enabled)
        self.viewport().update()

    def _on_completion_changed(
        self, _cal_id: str, _uid: str, _dtstart_utc: int
    ) -> None:
        self.refresh()

    def set_week_start(self, week_start: str) -> None:
        if week_start == self._week_start_pref:
            return
        self._week_start_pref = week_start
        self._start = start_of_week(self._start, week_start)
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
        self._now_line.refresh()
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh(data_dirty=False)

    def zoom_in(self) -> None:
        self.set_px_per_hour(self._px_per_hour + 8)

    def zoom_out(self) -> None:
        self.set_px_per_hour(self._px_per_hour - 8)

    def zoom_reset(self) -> None:
        self.set_px_per_hour(DEFAULT_PX_PER_HOUR)

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
        self._needs_scroll = True
        self.refresh()

    def go_today(self) -> None:
        today = date.today()
        self._start = start_of_week(today, self._week_start_pref)
        self._needs_scroll = True
        self.refresh()

    def refresh_theme(self) -> None:
        self._scene.update()
        self.viewport().update()

    def go_to_date(self, d: date) -> None:
        self._start = start_of_week(d, self._week_start_pref)
        self._needs_scroll = True
        self.refresh()

    def range_label(self) -> str:
        end = self._start + timedelta(days=self._day_count - 1)
        if self._start.month == end.month:
            return f"{self._start.strftime('%B %-d')}–{end.strftime('%-d, %Y')}"
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self, *, data_dirty: bool = True) -> None:
        if not data_dirty and self._cached_data is not None:
            col_w = max(
                20.0,
                (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count,
            )
            plan = _compute_week_placements(
                self._cached_data, self._px_per_hour, self._time_format, col_w
            )
            self._apply_plan(plan)
            return
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        start = self._start
        day_count = self._day_count
        px_per_hour = self._px_per_hour
        time_format = self._time_format
        col_w = max(
            20.0, (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / day_count
        )
        cal_info_snap = self._cal_info_provider()
        self._refresh_task = asyncio.ensure_future(
            self._refresh_async(
                start, day_count, px_per_hour, time_format, col_w, cal_info_snap
            )
        )

    async def _refresh_async(
        self,
        start: date,
        day_count: int,
        px_per_hour: int,
        time_format: str,
        col_w: float,
        cal_info_snap: dict,
    ) -> None:
        try:
            data = await asyncio.to_thread(
                _query_week_data, self._store, start, day_count, cal_info_snap
            )
        except asyncio.CancelledError:
            return
        if data is None:
            return
        self._cached_data = data
        plan = _compute_week_placements(data, px_per_hour, time_format, col_w)
        self._apply_plan(plan)

    # ── Chip placement ───────────────────────────────────────────────────

    def _apply_plan(self, plan: dict) -> None:
        if self._rendered_start != self._start:
            self._rebuild_grid()
        band_h = plan["band_h"]
        if abs(band_h - self._grid.all_day_band_h) > 0.5:
            self._grid.set_all_day_band_h(band_h)
            self._sticky.set_all_day_band_h(band_h)
            self._scene.setSceneRect(self._grid.boundingRect())
            self._now_line.refresh()
        else:
            self._sticky.set_all_day_band_h(band_h)

        completions: frozenset = plan.get("completions", frozenset())
        new_placements = plan["new_placements"]
        new_cluster_placements = plan.get("new_cluster_placements", {})
        read_only_cal_ids = {
            cid
            for cid, ci in self._cal_info_provider().items()
            if getattr(ci, "read_only", False)
        }
        old_chips = self._chips
        new_chips: dict[tuple[str, str, str], EventChip] = {}
        for key, chip in old_chips.items():
            if key not in new_placements:
                p = chip.parentItem()
                if p is not None:  # type: ignore[reportUnnecessaryComparison]
                    chip.setParentItem(None)  # type: ignore[reportArgumentType]
                if chip.scene() is self._scene:
                    self._scene.removeItem(chip)
        for key, pl in new_placements.items():
            is_sticky = pl["is_sticky"]
            inst_key = pl.get("inst_key")
            is_comp = inst_key in completions if inst_key else False
            if key in old_chips:
                chip = old_chips[key]
                chip.update_event_data(
                    pl["event"], completed=is_comp, inst_key=inst_key
                )
                chip.update_layout(
                    pl["rect"],
                    calendar_color=pl["calendar_color"],
                    time_prefix=pl["time_prefix"],
                    show_time_prefix=pl["show_time_prefix"],
                    instance_dtstart=pl["instance_dtstart"],
                    mode=self._chip_mode,
                )
                if is_sticky and chip.scene() is self._scene:
                    self._scene.removeItem(chip)
                    chip.setParentItem(self._sticky)
                elif not is_sticky and chip.parentItem() is not None:  # pyright: ignore[reportUnnecessaryComparison]
                    chip.setParentItem(None)  # type: ignore[reportArgumentType]
                    self._scene.addItem(chip)
            else:
                chip = EventChip(
                    pl["event"],
                    pl["rect"],
                    calendar_color=pl["calendar_color"],
                    mode=self._chip_mode,
                    show_time_prefix=pl["show_time_prefix"],
                    time_prefix=pl["time_prefix"],
                    time_format=self._time_format,
                    instance_dtstart=pl["instance_dtstart"],
                    completed=is_comp,
                    inst_key=inst_key,
                    read_only=pl["event"].calendar_id in read_only_cal_ids,
                )
                self._wire_chip_signals(chip)
                if is_sticky:
                    chip.setParentItem(self._sticky)
                else:
                    self._scene.addItem(chip)
            chip.set_completed_display(self._completed_enabled)
            new_chips[key] = chip
        self._chips = new_chips

        # Cluster placement (dense-overlap groups).
        old_clusters = self._clusters
        new_clusters: dict[tuple, LineCluster] = {}
        for key, cluster in old_clusters.items():
            if key not in new_cluster_placements and cluster.scene() is self._scene:
                self._scene.removeItem(cluster)
        for key, cpl in new_cluster_placements.items():
            rect = cpl["rect"]
            if key in old_clusters:
                cluster = old_clusters[key]
                cluster.update_layout(
                    rect,
                    cpl["cluster_data"],
                    cpl["px_per_hour"],
                    calendar_color_map=cpl["calendar_color_map"],
                    time_format=cpl["time_format"],
                    read_only_cal_ids=cpl["read_only_cal_ids"],
                )
            else:
                cluster = LineCluster(
                    rect,
                    cpl["cluster_data"],
                    cpl["px_per_hour"],
                    calendar_color_map=cpl["calendar_color_map"],
                    time_format=cpl["time_format"],
                    read_only_cal_ids=cpl["read_only_cal_ids"],
                )
                self._wire_cluster_signals(cluster)
                self._scene.addItem(cluster)
            cluster.setPos(rect.x(), rect.y())
            new_clusters[key] = cluster
        self._clusters = new_clusters

        self._band_popover_events = plan.get("band_popover_events", {})
        self._band_dense_cols = plan.get("band_dense_cols", set())
        self._current_band_h = plan.get("band_h", float(ALL_DAY_BAND_MIN))

        if self._needs_scroll:
            self._needs_scroll = False
            first_min = plan["first_event_minutes"]
            target_minutes = (
                first_min if first_min is not None else WORK_START_HOUR * 60
            )
            target_y = target_minutes * self._px_per_hour / 60
            sb = self.verticalScrollBar()
            sb.setValue(max(0, min(sb.maximum(), int(target_y - 8))))

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
        chip.toggle_complete_requested.connect(self._on_toggle_complete_requested)
        chip.drag_progress.connect(self._on_chip_drag_progress)
        chip.drag_committed.connect(self._on_chip_drag_committed)
        chip.drag_cancelled.connect(self._on_chip_drag_cancelled)
        chip.hovered.connect(self._on_event_hovered)
        chip.hover_left.connect(self._on_event_hover_left)

    def _wire_cluster_signals(self, cluster: "LineCluster") -> None:
        # Wire the embedded dominant chip's click signals exactly like a
        # regular chip; hover signals on the embedded chip are inert because
        # LineCluster sets setAcceptHoverEvents(False) on it.
        chip = cluster.chip
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
        cluster.hovered.connect(
            lambda evs, cl=cluster: self._on_cluster_hovered(evs, cl)
        )
        cluster.bar_hovered.connect(
            lambda ev_dict, cl=cluster: self._on_cluster_bar_hovered(ev_dict, cl)
        )
        cluster.hover_left.connect(self._on_event_hover_left)
        cluster.event_details_requested.connect(
            lambda ev_dict: self._on_details_requested(
                ev_dict["payload"]["event"],
                ev_dict["payload"].get("instance_dtstart"),
            )
        )
        cluster.event_edit_requested.connect(
            lambda ev_dict: self._on_edit_requested(
                ev_dict["payload"]["event"],
                ev_dict["payload"].get("instance_dtstart"),
            )
        )
        cluster.event_delete_requested.connect(
            lambda ev_dict: self._on_delete_requested(
                ev_dict["payload"]["event"],
                ev_dict["payload"].get("instance_dtstart"),
            )
        )

    def _on_toggle_complete_requested(
        self, inst_key: tuple[str, str, int], completed: bool
    ) -> None:
        cal_id, uid, dtstart_utc = inst_key
        self._store.set_completed(cal_id, uid, dtstart_utc, completed)

    # ── Band hover popover ───────────────────────────────────────────────

    def _band_col_at_vp(self, vp_pos: "QPoint") -> int | None:
        """Return the all-day band column under viewport position, or None."""
        vp_y = vp_pos.y()
        header_h = self._sticky.header_h()
        if not (DAY_HEADER_H <= vp_y < header_h):
            return None
        col_w = max(
            1.0,
            (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count,
        )
        col = int((vp_pos.x() - TIME_AXIS_WIDTH) / col_w)
        if col < 0 or col >= self._day_count:
            return None
        return col

    def _update_band_hover(self, vp_pos: "QPoint") -> None:
        col = self._band_col_at_vp(vp_pos)
        if col != self._hovered_band_col:
            self._hovered_band_col = col
            self._band_show_timer.stop()
            self._popover.hide()
            if col is not None and col in self._band_dense_cols:
                self._pending_band_col = col
                self._band_show_timer.start(280)

    def _show_band_popover(self) -> None:
        col = self._pending_band_col
        if col is None or col != self._hovered_band_col:
            return
        events = self._band_popover_events.get(col)
        if not events:
            return
        day = self._start + timedelta(days=col)
        col_w = max(
            1.0,
            (self._grid.boundingRect().width() - TIME_AXIS_WIDTH) / self._day_count,
        )
        vp_x = int(TIME_AXIS_WIDTH + (col + 1) * col_w)
        vp_y = int(DAY_HEADER_H)
        global_pt: QPoint = self.viewport().mapToGlobal(QPoint(vp_x, vp_y))
        self._popover.show_for_day(day, events, global_pt)

    # ── Inspector pane (hover → right-side details panel) ────────────────

    def _on_event_hovered(self, popover_event: PopoverEvent, notes: str | None) -> None:
        if self._inspector is not None:
            self._inspector.show_event(popover_event, notes)

    def _on_event_hover_left(self) -> None:
        if self._inspector is not None:
            self._inspector.clear()

    def _on_cluster_hovered(self, events: list, cluster: "LineCluster") -> None:
        if self._inspector is None:
            return
        popover_events = cluster_events_to_popover_events(events, self._time_format)
        if not popover_events:
            return
        dom_idx = cluster._cluster_data.get("dominant_index", 0)  # noqa: SLF001
        try:
            primary_payload = events[dom_idx]
        except IndexError:
            primary_payload = events[0]
        primary_uid = primary_payload["payload"]["event"].uid
        primary = next(
            (pe for pe in popover_events if pe.uid == primary_uid),
            popover_events[0],
        )
        self._inspector.show_cluster(primary, popover_events)

    def _on_cluster_bar_hovered(
        self, ev_dict: dict, cluster: LineCluster
    ) -> None:
        if self._inspector is None:
            return
        all_events = cluster.cluster_events
        popover_events = cluster_events_to_popover_events(all_events, self._time_format)
        if not popover_events:
            return
        bar_uid = ev_dict["payload"]["event"].uid
        primary = next(
            (pe for pe in popover_events if pe.uid == bar_uid),
            popover_events[0],
        )
        self._inspector.show_cluster(primary, popover_events)

    # ── Drag geometry helpers ─────────────────────────────────────────────

    def _snap_minutes_to(self, m: float) -> int:
        snap = self._snap_minutes
        return max(0, min(1440, round(m / snap) * snap))

    def _snap_minutes_floor(self, m: float) -> int:
        snap = self._snap_minutes
        return max(0, min(1440, math.floor(m / snap) * snap))

    def _snap_minutes_ceil(self, m: float) -> int:
        snap = self._snap_minutes
        return max(0, min(1440, math.ceil(m / snap) * snap))

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
        s = fmt_hm(sh, sm, self._time_format)
        e = fmt_hm(eh, em, self._time_format)
        time_str = f"{s} – {e}"
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

        # Day-label column-header row: navigate to Day view for the clicked date.
        if vp_y < DAY_HEADER_H:
            scene_x = self.mapToScene(vp_pos).x()
            day_off = self._scene_x_to_day_offset(scene_x)
            if day_off is not None:
                self.day_header_activated.emit(self._start + timedelta(days=day_off))
                event.accept()
                return
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(vp_pos)

        # If the click landed on a chip, let Qt dispatch it to the chip.
        item = self._scene.itemAt(scene_pos, self.viewportTransform())
        if isinstance(item, (EventChip, LineCluster)):
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
            press_min = self._scene_y_to_minutes(scene_pos.y())
            self._drag_kind = "create_body"
            self._drag_day_offset = day_off
            self._drag_start_min = press_min
            self._drag_current_min = press_min
            self._press_scene_pos = scene_pos
        event.accept()

    @override
    def leaveEvent(self, event) -> None:  # noqa: ANN001
        super().leaveEvent(event)
        self._hovered_band_col = None
        self._pending_band_col = None
        self._band_show_timer.stop()
        self._popover.hide()

    @override
    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        self._update_band_hover(event.pos())
        if self._drag_kind is None:
            super().mouseMoveEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())

        if self._drag_kind == "create_body":
            current_min = self._scene_y_to_minutes(scene_pos.y())
            current_day = self._scene_x_to_day_offset(scene_pos.x())
            if current_day is None:
                current_day = self._drag_day_offset
            self._drag_current_min = current_min
            lo = min(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            hi = max(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            start_min = self._snap_minutes_floor(lo)
            end_min = self._snap_minutes_ceil(hi)
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
            s = fmt_hm(sh, sm, self._time_format)
            e = fmt_hm(eh, em, self._time_format)
            label = f"{s} – {e}  {dur_str}"
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
            current_min = self._scene_y_to_minutes(scene_pos.y())
            lo = min(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            hi = max(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            if hi - lo < self._snap_minutes / 2:  # treat as click
                start_min = self._snap_minutes_floor(self._drag_start_min)  # type: ignore[reportArgumentType]
                end_min = start_min + 60
            else:
                start_min = self._snap_minutes_floor(lo)
                end_min = self._snap_minutes_ceil(hi)
                if end_min <= start_min:
                    end_min = start_min + self._snap_minutes
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
                for chip in self._chips.values():
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
            for chip in self._chips.values():
                if chip._event is event:  # type: ignore[reportPrivateUsage]
                    r = chip.sceneBoundingRect()
                    self._press_scene_pos = chip._press_scene_pos  # type: ignore[reportPrivateUsage]
                    origin_day = int((r.left() - TIME_AXIS_WIDTH) / col_w)
                    if event.all_day:
                        self._drag_chip_origin = (origin_day, 0, 0)
                        self._drag_chip_grab_offset_min = 0.0
                    else:
                        origin_start = int((r.top() - body_top) * 60 / pph)
                        origin_end = int((r.bottom() - body_top) * 60 / pph)
                        self._drag_chip_origin = (origin_day, origin_start, origin_end)
                        if self._press_scene_pos is not None:
                            self._drag_chip_grab_offset_min = (
                                self._scene_y_to_minutes(self._press_scene_pos.y())
                                - origin_start
                            )
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
            grab = self._drag_chip_grab_offset_min or 0.0
            new_start = self._snap_minutes_to(cursor_min - grab)
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
            self._drag_chip_grab_offset_min = None
            self._press_scene_pos = None
            return

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            grab = self._drag_chip_grab_offset_min or 0.0
            new_start = self._snap_minutes_to(cursor_min - grab)
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
        self._drag_chip_grab_offset_min = None
        self._press_scene_pos = None

    def _on_chip_drag_cancelled(self, event) -> None:
        self._teardown_preview()
        self._drag_chip_event = None
        self._drag_chip_mode = None
        self._drag_chip_origin = None
        self._drag_chip_grab_offset_min = None
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


