from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen
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
from lilical.ui._time_fmt import fmt_hm, fmt_hour_label
from lilical.ui.views._multi_day import multi_day_span
from lilical.ui.views._overlap import pack_overlapping_lanes
from lilical.ui.widgets._popover_rows import (
    PopoverEvent,
    cluster_events_to_popover_events,
)
from lilical.ui.widgets.drag_preview import DragPreview
from lilical.ui.widgets.event_chip import ChipMode, EventChip
from lilical.ui.widgets.event_tooltip import EventTooltip
from lilical.ui.widgets.inspector_pane import InspectorPane
from lilical.ui.widgets.line_cluster import LineCluster
from lilical.utils.timezone import local_iana_tz, local_zoneinfo

log = logging.getLogger(__name__)

ALL_DAY_MAX_ROWS = 4
HOURS = 24

_BASE_TIME_AXIS_WIDTH = 60
_BASE_DAY_HEADER_H = 40
_BASE_ALL_DAY_ROW_H = 26
_BASE_ALL_DAY_BAND_MIN = 34
_BASE_DEFAULT_PX_PER_HOUR = 64
_BASE_PX_PER_HOUR_MIN = 24
_BASE_PX_PER_HOUR_MAX = 120

TIME_AXIS_WIDTH = _BASE_TIME_AXIS_WIDTH
DAY_HEADER_H = _BASE_DAY_HEADER_H
ALL_DAY_ROW_H = _BASE_ALL_DAY_ROW_H
ALL_DAY_BAND_MIN = _BASE_ALL_DAY_BAND_MIN
DEFAULT_PX_PER_HOUR = _BASE_DEFAULT_PX_PER_HOUR
PX_PER_HOUR_MIN = _BASE_PX_PER_HOUR_MIN
PX_PER_HOUR_MAX = _BASE_PX_PER_HOUR_MAX


def apply_scale(factor: float) -> None:
    g = globals()
    g["TIME_AXIS_WIDTH"] = max(1, round(_BASE_TIME_AXIS_WIDTH * factor))
    g["DAY_HEADER_H"] = max(1, round(_BASE_DAY_HEADER_H * factor))
    g["ALL_DAY_ROW_H"] = max(1, round(_BASE_ALL_DAY_ROW_H * factor))
    g["ALL_DAY_BAND_MIN"] = max(1, round(_BASE_ALL_DAY_BAND_MIN * factor))
    g["DEFAULT_PX_PER_HOUR"] = max(1, round(_BASE_DEFAULT_PX_PER_HOUR * factor))
    g["PX_PER_HOUR_MIN"] = max(1, round(_BASE_PX_PER_HOUR_MIN * factor))
    g["PX_PER_HOUR_MAX"] = max(1, round(_BASE_PX_PER_HOUR_MAX * factor))

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
        self._time_format = "24h"
        self.setZValue(-10)

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

    def set_time_format(self, fmt: str) -> None:
        self._time_format = fmt
        self.update()
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
            painter.drawLine(int(TIME_AXIS_WIDTH), int(y), int(self._width), int(y))
        # Half-hour dotted lines.
        if self._px_per_hour >= 40:
            painter.setPen(
                QPen(QColor(theme.BORDER).darker(125), 1, Qt.PenStyle.DotLine)
            )
            for hour in range(HOURS):
                y = body_top + hour * self._px_per_hour + self._px_per_hour / 2
                painter.drawLine(int(TIME_AXIS_WIDTH), int(y), int(self._width), int(y))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(
            int(TIME_AXIS_WIDTH),
            int(body_top),
            int(TIME_AXIS_WIDTH),
            int(self.grid_height()),
        )

        # Hour labels.
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_TIME_AXIS))
        for hour in range(HOURS):
            y = body_top + hour * self._px_per_hour
            painter.drawText(
                QRectF(0, y - 8, TIME_AXIS_WIDTH - 6, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                fmt_hour_label(hour, self._time_format),
            )


class _DayNowLine(QGraphicsItem):
    """Current-time indicator for Day view, drawn above event chips (z=50)."""

    def __init__(self, grid: DayGrid) -> None:
        super().__init__()
        self._grid = grid
        self._y = 0.0
        self.setZValue(50)

    def set_grid(self, grid: DayGrid) -> None:
        self._grid = grid
        self.refresh()

    def refresh(self) -> None:
        today = date.today()
        self.prepareGeometryChange()
        if today == self._grid._day:
            now = datetime.now().astimezone()
            minutes = now.hour * 60 + now.minute
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
        w = self._grid._width
        line_color = QColor(theme.DANGER)
        line_color.setAlphaF(0.65)
        painter.setPen(QPen(line_color, 2))
        painter.drawLine(int(TIME_AXIS_WIDTH), int(self._y), int(w), int(self._y))
        painter.setBrush(QColor(theme.DANGER))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(TIME_AXIS_WIDTH - 4, self._y - 4, 8, 8))


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
        painter.drawLine(int(TIME_AXIS_WIDTH), 0, int(TIME_AXIS_WIDTH), int(body_top))

        # Strong borders.
        painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        painter.drawLine(0, int(DAY_HEADER_H), int(self._width), int(DAY_HEADER_H))
        painter.drawLine(0, int(body_top), int(self._width), int(body_top))

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


def _query_day_data(store, day: date, cal_info_snap: dict) -> dict | None:
    """Off-thread: query DB for the day view."""
    start_dt = _local_midnight(day)
    end_dt = start_dt + timedelta(hours=28)
    visible_ids = {ci.id for ci in cal_info_snap.values() if ci.visible}
    try:
        instances = store.list_instances(start_dt, end_dt, calendar_ids=visible_ids)
    except Exception:
        log.exception("DayView: failed to query instances")
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
        "day": day,
        "completions": completions,
    }


def _compute_day_placements(
    data: dict, col_w: float, px_per_hour: int, time_format: str
) -> dict:
    """Pure geometry: compute chip placement rects from raw day data."""
    instances = data["instances"]
    events = data["events"]
    cal_color = data["cal_color"]
    read_only_cal_ids: set[str] = data.get("read_only_cal_ids", set())
    day = data["day"]
    completions: frozenset = data.get("completions", frozenset())

    # Count band occupants: true all-day events and multi-day timed events covering this day.  # noqa: E501
    all_day_count = 0
    for inst in instances:
        span = multi_day_span(inst)
        if span is not None:
            if span[0] <= day <= span[1]:
                all_day_count += 1
        elif inst.all_day and _is_on(inst, day):
            all_day_count += 1
    rows_shown = min(all_day_count, ALL_DAY_MAX_ROWS)
    band_h = float(
        ALL_DAY_BAND_MIN if rows_shown == 0 else (4 + rows_shown * ALL_DAY_ROW_H)
    )
    body_top = DAY_HEADER_H + band_h

    first_event_minutes: int | None = None
    all_day_idx = 0
    timed_bucket: list[tuple[float, float, str, dict]] = []
    new_placements: dict[tuple, dict] = {}

    for inst in instances:
        event = events.get(id(inst))
        if event is None:
            continue
        try:
            t = datetime.fromisoformat(inst.dtstart_local).astimezone()
        except (ValueError, TypeError):
            continue

        span = multi_day_span(inst)
        if span is None and inst.all_day:
            if t.date() != day:
                continue
            key = (inst.calendar_id, inst.uid, inst.dtstart_local)
            if all_day_idx >= ALL_DAY_MAX_ROWS:
                all_day_idx += 1
                continue
            y = DAY_HEADER_H + 2 + all_day_idx * ALL_DAY_ROW_H
            h = ALL_DAY_ROW_H - 2
            all_day_idx += 1
            new_placements[key] = {
                "rect": QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, h),
                "calendar_color": cal_color.get(inst.calendar_id),
                "show_time_prefix": False,
                "time_prefix": None,
                "continues_left": False,
                "continues_right": False,
                "overlap_cols": 1,
                "instance_dtstart": t,
                "is_sticky": True,
                "event": event,
                "inst_key": (inst.calendar_id, inst.uid, inst.dtstart_utc),
            }
            continue
        if span:
            start_day, end_day = span
            if not (start_day <= day <= end_day):
                continue
            key = (
                inst.calendar_id,
                inst.uid,
                inst.dtstart_local,
                "band",
                day.isoformat(),
            )
            if all_day_idx < ALL_DAY_MAX_ROWS:
                y = DAY_HEADER_H + 2 + all_day_idx * ALL_DAY_ROW_H
                h = ALL_DAY_ROW_H - 2
                new_placements[key] = {
                    "rect": QRectF(TIME_AXIS_WIDTH + 1, y, col_w - 2, h),
                    "calendar_color": cal_color.get(inst.calendar_id),
                    "show_time_prefix": False,
                    "time_prefix": None,
                    "continues_left": day > start_day,
                    "continues_right": day < end_day,
                    "overlap_cols": 1,
                    "instance_dtstart": t,
                    "is_sticky": True,
                    "event": event,
                    "inst_key": (inst.calendar_id, inst.uid, inst.dtstart_utc),
                }
            all_day_idx += 1
            continue

        # Timed event: single-day, or cross-midnight split (slice visible today).
        try:
            end_t = datetime.fromisoformat(inst.dtend_local).astimezone()
        except (ValueError, TypeError):
            end_t = t
        start_day = t.date()
        end_day = end_t.date()
        crosses_midnight = end_day > start_day and (
            end_t.hour != 0 or end_t.minute != 0
        )
        if start_day == day:
            # Event starts today.
            s_min = t.hour * 60 + t.minute
            if not crosses_midnight:
                e_min = (
                    1440 if end_day > start_day else end_t.hour * 60 + end_t.minute
                )
                if e_min <= s_min:
                    e_min = s_min + 15
                c_left, c_right, show_pfx = False, False, True
            else:
                e_min = 1440
                c_left, c_right, show_pfx = False, True, True
            day_off = 0
        elif crosses_midnight and end_day == day:
            # Tail: event started yesterday, ends today.
            s_min = 0
            e_min = end_t.hour * 60 + end_t.minute
            if e_min == 0:
                continue  # ends exactly at midnight — no tail chip needed
            c_left, c_right, show_pfx = True, False, False
            day_off = 1
        else:
            continue
        if first_event_minutes is None or s_min < first_event_minutes:
            first_event_minutes = s_min
        key = (inst.calendar_id, inst.uid, inst.dtstart_local, day_off)
        timed_bucket.append(
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

    new_cluster_placements: dict = {}
    if timed_bucket:
        is_own_fn = (
            (lambda c: c not in read_only_cal_ids) if read_only_cal_ids else None
        )
        packed = pack_overlapping_lanes(timed_bucket, col_w, is_own_calendar_fn=is_own_fn)  # noqa: E501
        _tfmt = "%-I:%M %p" if time_format == "12h" else "%H:%M"
        for x_off, chip_w, mode, payload_or_cluster in packed:
            if mode == "normal":
                payload = payload_or_cluster
                bucket_entry = next(
                    (b for b in timed_bucket if b[3] is payload), None
                )
                if bucket_entry is None:
                    continue
                start_min_f, end_min_f = bucket_entry[0], bucket_entry[1]
                chip_x = TIME_AXIS_WIDTH + x_off
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
                cluster_data = payload_or_cluster
                cluster_start_min = cluster_data["cluster_start_min"]
                cluster_end_min = cluster_data["cluster_end_min"]
                cluster_x = TIME_AXIS_WIDTH + x_off
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
                }

    return {
        "new_placements": new_placements,
        "new_cluster_placements": new_cluster_placements,
        "band_h": band_h,
        "first_event_minutes": first_event_minutes,
        "completions": completions,
    }


def _build_mini_agenda_plan(
    store, now: datetime, count: int, cal_info_snap: dict, time_format: str = "24h"
) -> list[dict]:
    """Off-thread: query DB and build mini-agenda item data."""
    end = now + timedelta(days=14)
    visible_ids = {ci.id for ci in cal_info_snap.values() if ci.visible}
    try:
        instances = store.list_instances(now, end, calendar_ids=visible_ids)
    except Exception:
        log.exception("DayView mini-agenda: failed to query instances")
        return []

    cal_color: dict[str, str | None] = {
        ci.id: ci.color for ci in cal_info_snap.values()
    }

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

    mini_instances = [inst for _t, inst in upcoming[:count]]
    mini_events = store.events_for_instances(mini_instances)  # type: ignore[arg-type]

    items: list[dict] = []
    for t, inst in upcoming[:count]:
        event = mini_events.get(id(inst))
        if event is None:
            continue
        color_hint = event.color or cal_color.get(inst.calendar_id)  # type: ignore[attr-defined]
        hm = fmt_hm(t.hour, t.minute, time_format)
        when = f"Today {hm}" if now.date() == t.date() else f"{t.strftime('%a')} {hm}"
        if inst.all_day:  # type: ignore[attr-defined]
            when = t.strftime("%a") if t.date() != now.date() else "Today"
            when = f"{when}  (all day)"
        label = f"{when}    {event.summary or '(no title)'}"
        if event.location:
            label += f"   · {event.location}"
        items.append({"label": label, "color": color_hint})
    return items


class _DayCanvas(QGraphicsView):
    """Graphics canvas portion of the Day view (the time-grid)."""

    def __init__(
        self,
        store: EventStore,
        day: date,
        cal_info_provider=None,
        *,
        inspector: InspectorPane | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._cached_data: dict | None = None
        self._day = day
        self._inspector = inspector
        self._tooltip = EventTooltip()
        self._px_per_hour = DEFAULT_PX_PER_HOUR
        self._chip_mode: ChipMode = ChipMode.BARS
        self._time_format: str = "24h"
        self._chips: dict[tuple, EventChip] = {}
        self._clusters: dict[tuple, LineCluster] = {}
        self._refresh_task: asyncio.Task | None = None
        self._rendered_day: date | None = None
        self._needs_scroll: bool = True
        self._completed_enabled: bool = False
        # Drag-to-create / move / resize state
        self._snap_minutes: int = 15
        self._drag_kind: str | None = None
        self._drag_start_min: float | None = None
        self._drag_current_min: float | None = None
        self._drag_chip_event = None
        self._drag_chip_mode: str | None = None
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
        # viewport width so the layout doesn't get pushed wider.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(0)

        # Initial widths are placeholders — resizeEvent fills in viewport size.
        self._grid = DayGrid(self._day, 1, px_per_hour=self._px_per_hour)
        self._scene.addItem(self._grid)
        self._sticky = _DayStickyHeader(self._day, 1)
        self._scene.addItem(self._sticky)
        self._now_line = _DayNowLine(self._grid)
        self._scene.addItem(self._now_line)
        self._now_line.refresh()
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_day = self._day

        self._now_timer = QTimer(self)
        self._now_timer.timeout.connect(self._now_line.refresh)
        self._now_timer.start(60_000)

        # Keep the sticky header pinned to viewport-top as the user scrolls.
        self.verticalScrollBar().valueChanged.connect(self._on_v_scroll)

        store.instance_completion_changed.connect(self._on_completion_changed)

    @override
    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        w = self.viewport().width()
        self._grid.set_width(w)
        self._sticky.set_width(w)
        self._now_line.refresh()
        self._scene.setSceneRect(0, 0, w, self._grid.grid_height())
        self.refresh(data_dirty=False)

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
        self._now_line.set_grid(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self._rendered_day = self._day

    def set_day(self, d: date) -> None:
        self._day = d
        self._needs_scroll = True
        self.refresh()

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

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
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

    def set_time_format(self, fmt: str) -> None:
        if fmt == self._time_format:
            return
        self._time_format = fmt
        self._grid.set_time_format(fmt)
        self.refresh(data_dirty=False)
        self._refresh_mini_agenda()

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def refresh(self, *, data_dirty: bool = True) -> None:
        if not data_dirty and self._cached_data is not None:
            col_w = max(20.0, self._grid.boundingRect().width() - TIME_AXIS_WIDTH)
            plan = _compute_day_placements(
                self._cached_data, col_w, self._px_per_hour, self._time_format
            )
            self._apply_plan(plan)
            return
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        day = self._day
        col_w = max(20.0, self._grid.boundingRect().width() - TIME_AXIS_WIDTH)
        px_per_hour = self._px_per_hour
        time_format = self._time_format
        cal_info_snap = self._cal_info_provider()
        self._refresh_task = asyncio.ensure_future(
            self._refresh_async(day, col_w, px_per_hour, time_format, cal_info_snap)
        )

    async def _refresh_async(
        self,
        day: date,
        col_w: float,
        px_per_hour: int,
        time_format: str,
        cal_info_snap: dict,
    ) -> None:
        try:
            data = await asyncio.to_thread(
                _query_day_data, self._store, day, cal_info_snap
            )
        except asyncio.CancelledError:
            return
        if data is None:
            return
        self._cached_data = data
        plan = _compute_day_placements(data, col_w, px_per_hour, time_format)
        self._apply_plan(plan)

    def _apply_plan(self, plan: dict) -> None:
        if self._rendered_day != self._day:
            self._rebuild_grid()
        band_h: float = plan["band_h"]
        new_placements: dict = plan["new_placements"]

        if abs(band_h - self._grid.all_day_band_h) > 0.5:
            self._grid.set_all_day_band_h(band_h)
            self._sticky.set_all_day_band_h(band_h)
            self._scene.setSceneRect(self._grid.boundingRect())
            self._now_line.refresh()
        else:
            self._sticky.set_all_day_band_h(band_h)

        completions: frozenset = plan.get("completions", frozenset())
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

    def _on_copy_requested(self, event, instance_dtstart=None) -> None:
        from lilical.ui.views._recurrence_actions import open_copy_dialog

        open_copy_dialog(self.parent(), self._store, event, instance_dtstart)  # type: ignore[reportArgumentType]

    # ── Snap / public setter ──────────────────────────────────────────────

    def set_snap_minutes(self, m: int) -> None:
        if m not in (5, 10, 15, 30, 60):
            m = 15
        self._snap_minutes = m

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
        chip.copy_requested.connect(
            lambda ev, c=chip: self._on_copy_requested(ev, c.instance_dtstart)
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
        chip.copy_requested.connect(
            lambda ev, c=chip: self._on_copy_requested(ev, c.instance_dtstart)
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
        cluster.event_copy_requested.connect(
            lambda ev_dict: self._on_copy_requested(
                ev_dict["payload"]["event"],
                ev_dict["payload"].get("instance_dtstart"),
            )
        )

    # ── Inspector pane (hover → right-side details panel) ────────────────

    def _on_event_hovered(self, popover_event: PopoverEvent, notes: str | None) -> None:
        if self._inspector is not None and self._inspector.isVisible():
            self._inspector.show_event(popover_event, notes)
        else:
            self._tooltip.show_event(popover_event, notes, QCursor.pos())

    def _on_event_hover_left(self) -> None:
        self._tooltip.hide_tooltip()
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
        if self._inspector.isVisible():
            self._inspector.show_cluster(primary, popover_events)
        else:
            self._tooltip.show_cluster(primary, popover_events, QCursor.pos())

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
        if self._inspector.isVisible():
            self._inspector.show_cluster(primary, popover_events)
        else:
            self._tooltip.show_cluster(primary, popover_events, QCursor.pos())

    def _on_toggle_complete_requested(
        self, inst_key: tuple[str, str, int], completed: bool
    ) -> None:
        cal_id, uid, dtstart_utc = inst_key
        self._store.set_completed(cal_id, uid, dtstart_utc, completed)

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
        if isinstance(item, (EventChip, LineCluster)):
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
            press_min = self._scene_y_to_minutes(scene_pos.y())
            self._drag_kind = "create_body"
            self._drag_start_min = press_min
            self._drag_current_min = press_min
            self._press_scene_pos = scene_pos
        event.accept()

    @override
    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_kind is None:
            super().mouseMoveEvent(event)
            return

        scene_pos = self.mapToScene(event.pos())

        if self._drag_kind == "create_body":
            current_min = self._scene_y_to_minutes(scene_pos.y())
            self._drag_current_min = current_min
            lo = min(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            hi = max(self._drag_start_min, current_min)  # type: ignore[reportArgumentType]
            start_min = self._snap_minutes_floor(lo)
            end_min = self._snap_minutes_ceil(hi)
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
            s = fmt_hm(sh, sm, self._time_format)
            e = fmt_hm(eh, em, self._time_format)
            label = f"{s} – {e}  {dur_str}"
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
        body_top = self._grid.hour_top()

        if event.all_day:
            return

        if self._drag_chip_event is None:
            self._drag_chip_event = event
            self._drag_chip_mode = mode
            for chip in self._chips.values():
                if chip._event is event:  # type: ignore[reportPrivateUsage]
                    r = chip.sceneBoundingRect()
                    self._press_scene_pos = chip._press_scene_pos  # type: ignore[reportPrivateUsage]
                    origin_start = int((r.top() - body_top) * 60 / pph)
                    origin_end = int((r.bottom() - body_top) * 60 / pph)
                    self._drag_chip_origin = (0, origin_start, origin_end)
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
        _origin_day, origin_start, origin_end = self._drag_chip_origin
        duration = origin_end - origin_start

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            grab = self._drag_chip_grab_offset_min or 0.0
            new_start = self._snap_minutes_to(cursor_min - grab)
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

        _origin_day, origin_start, origin_end = self._drag_chip_origin
        duration = origin_end - origin_start

        if mode == "move":
            cursor_min = self._scene_y_to_minutes(scene_pos.y())
            grab = self._drag_chip_grab_offset_min or 0.0
            new_start = self._snap_minutes_to(cursor_min - grab)
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


def _is_on(inst, d: date) -> bool:
    try:
        return datetime.fromisoformat(inst.dtstart_local).astimezone().date() == d
    except (ValueError, TypeError):
        return False


class DayView(QWidget):
    """Day view = time-grid canvas + mini-agenda strip (next 3 upcoming)."""

    MINI_AGENDA_COUNT = 3
    MINI_AGENDA_H = 96

    def __init__(
        self,
        store: EventStore,
        day: date | None = None,
        cal_info_provider=None,
        *,
        inspector: InspectorPane | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._day = day or date.today()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._canvas = _DayCanvas(
            store, self._day, cal_info_provider=cal_info_provider, inspector=inspector
        )
        layout.addWidget(self._canvas, 1)

        # Mini-agenda strip below the time grid.
        self._mini_label = QLabel("Upcoming")
        self._mini_label.setStyleSheet(
            f"padding: 4px 8px; color: {theme.TEXT_SECONDARY}; "
            f"background: {theme.BG_SURFACE}; "
            f"border-top: 1px solid {theme.BORDER_STRONG};"
        )
        self._mini_label.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_TIME_AXIS, QFont.Weight.Bold)
        )
        layout.addWidget(self._mini_label)

        self._mini_list = QListWidget()
        self._mini_list.setFixedHeight(self.MINI_AGENDA_H)
        self._mini_list.setStyleSheet(
            f"background: {theme.BG_BASE}; color: {theme.TEXT_PRIMARY}; border: none;"
        )
        layout.addWidget(self._mini_list)

        self._mini_refresh_task: asyncio.Task | None = None
        self._refresh_mini_agenda()

    # ── Public surface used by main_window / sidebar ─────────────────────

    def navigate(self, days: int) -> None:
        self._day = self._day + timedelta(days=days)
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

    def refresh(self, *, data_dirty: bool = True) -> None:
        self._canvas.refresh(data_dirty=data_dirty)
        self._refresh_mini_agenda()

    @override
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._canvas._cached_data is None:
            self._canvas.refresh()

    def refresh_theme(self) -> None:
        self._mini_label.setStyleSheet(
            f"padding: 4px 8px; color: {theme.TEXT_SECONDARY}; "
            f"background: {theme.BG_SURFACE}; "
            f"border-top: 1px solid {theme.BORDER_STRONG};"
        )
        self._mini_list.setStyleSheet(
            f"background: {theme.BG_BASE}; color: {theme.TEXT_PRIMARY}; border: none;"
        )
        self._canvas.viewport().update()
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

    def set_completed_events_enabled(self, enabled: bool) -> None:
        self._canvas.set_completed_events_enabled(enabled)

    # ── Mini-agenda ──────────────────────────────────────────────────────

    def _refresh_mini_agenda(self) -> None:
        if self._mini_refresh_task and not self._mini_refresh_task.done():
            self._mini_refresh_task.cancel()
        now = datetime.now().astimezone()
        cal_info_snap = self._cal_info_provider()
        self._mini_refresh_task = asyncio.ensure_future(
            self._mini_refresh_async(now, self.MINI_AGENDA_COUNT, cal_info_snap)
        )

    async def _mini_refresh_async(
        self, now: datetime, count: int, cal_info_snap: dict
    ) -> None:
        try:
            items = await asyncio.to_thread(
                _build_mini_agenda_plan,
                self._store,
                now,
                count,
                cal_info_snap,
                self._time_format,
            )
        except asyncio.CancelledError:
            return
        self._apply_mini_plan(items)

    def _apply_mini_plan(self, items: list[dict]) -> None:
        self._mini_list.clear()
        for item_data in items:
            item = QListWidgetItem(item_data["label"])
            color_hint = item_data["color"]
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
