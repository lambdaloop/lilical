from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QSizePolicy,
)

from lilical.storage.event_store import EventStore
from lilical.ui import theme
from lilical.ui.widgets.event_chip import ChipMode, EventChip

log = logging.getLogger(__name__)

CELL_W = 140
CELL_H = 100
HEADER_H = 24
COLS = 7
ROWS = 6
PAD = 4
CHIP_H = 16
CHIP_GAP = 2
TODAY_RING_RADIUS = 11


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

        # Weekend column tint (Sat/Sun): subtle.
        for c in (5, 6):  # Mon=0, so Sat=5, Sun=6
            painter.fillRect(
                QRectF(c * CELL_W, HEADER_H, CELL_W, ROWS * CELL_H),
                QColor(theme.BG_WEEKEND),
            )

        # Day-of-week strip.
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_MONTH_HEADER, QFont.Weight.Bold))
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
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_DAY_NUMBER, QFont.Weight.Bold))
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
                    QColor(theme.TEXT_PRIMARY) if in_month else QColor(theme.TEXT_DISABLED)
                )
                painter.drawText(x + 2, y + 14, str(cur.day))
                cur += timedelta(days=1)


class _OverflowChip(QGraphicsItem):
    """'+N more' indicator. Click switches view to the Day view of `for_day`."""

    def __init__(
        self,
        rect: QRectF,
        label: str,
        for_day: date,
        click_callback,
    ) -> None:
        super().__init__()
        self._rect = rect
        self._label = label
        self._for_day = for_day
        self._cb = click_callback
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION, QFont.Weight.Medium))
        painter.drawText(
            self._rect.adjusted(4, 0, -4, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._label,
        )

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton and self._cb is not None:
            self._cb(self._for_day)
            event.accept()
        else:
            super().mousePressEvent(event)


class MonthView(QGraphicsView):
    day_activated = Signal(object)   # emits date — for switching to Day view

    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        self._chip_mode: ChipMode = ChipMode.BARS
        self._chips: list[QGraphicsItem] = []
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
    def resizeEvent(self, event) -> None:  # noqa: ANN001
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

    def set_chip_mode(self, mode: ChipMode) -> None:
        if mode is self._chip_mode:
            return
        self._chip_mode = mode
        self.refresh()

    @property
    def chip_mode(self) -> ChipMode:
        return self._chip_mode

    def refresh(self) -> None:
        for item in self._chips:
            self._scene.removeItem(item)
        self._chips.clear()

        grid_start = self._grid.grid_start
        end_day = grid_start + timedelta(days=42)
        start_dt = _local_midnight(grid_start)
        end_dt = _local_midnight(end_day)

        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("MonthView: failed to query instances")
            return

        # Precompute calendar colours.
        cal_color: dict[str, str | None] = {}

        # Build per-day buckets, distinguishing multi-day from single-day.
        # An instance is "multi-day" when its end-date (exclusive at midnight)
        # is on a strictly later local date than its start.
        single_by_day: dict[date, list] = {}
        multi_spans: list[tuple[date, date, object]] = []  # (start, end_inclusive, inst)
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
                et = datetime.fromisoformat(inst.dtend_local).astimezone()
            except (ValueError, TypeError):
                continue

            start_day = t.date()
            end_day_inclusive = et.date()
            # Half-open: event ending at 00:00 of day N actually ends day N-1.
            ends_at_midnight = et.time().hour == 0 and et.time().minute == 0
            if ends_at_midnight and end_day_inclusive > start_day:
                end_day_inclusive = end_day_inclusive - timedelta(days=1)

            if end_day_inclusive > start_day:
                multi_spans.append((start_day, end_day_inclusive, inst))
            else:
                single_by_day.setdefault(start_day, []).append((t, inst))

        # ── Layout pass 1: place multi-day spans in row slots ────────────
        # Pack spans into row "tracks" so they don't visually overlap.
        # Maximum tracks per row equals max_chips_per_cell; spillover counts as
        # hidden and contributes to the cell's +N more.
        row_slots_used: dict[int, list[list[tuple[int, int]]]] = {}
        # row_slots_used[row_idx][track] = list of (start_col, end_col) spans

        def row_for_date(d: date) -> int | None:
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

        # Track which span occupies which row+track, so single-day chips can
        # avoid colliding.
        per_row_track_occupied: dict[tuple[int, int], list[tuple[int, int]]] = {}

        # Hidden span count contributed to (row, col) cells, for +N more.
        hidden_per_cell: dict[tuple[int, int], int] = {}

        # Stable order: longer spans first so they get prime tracks.
        multi_spans.sort(key=lambda x: ((x[1] - x[0]).days, x[0]), reverse=True)

        for s_day, e_day, inst in multi_spans:
            event = self._store.get_event(inst.uid, inst.calendar_id)
            if event is None:
                continue
            if inst.calendar_id not in cal_color:
                cal = self._store.get_calendar(inst.calendar_id)
                cal_color[inst.calendar_id] = cal.color if cal else None

            # Clip span to visible grid.
            visible_start = max(s_day, grid_start)
            visible_end = min(e_day, grid_start + timedelta(days=41))
            if visible_end < visible_start:
                continue

            # Render one chip per week-row.
            d = visible_start
            while d <= visible_end:
                rc = cell_row_col(d)
                if rc is None:
                    d += timedelta(days=1)
                    continue
                row, col = rc
                # End of this week-row segment.
                row_end_day = grid_start + timedelta(days=row * 7 + 6)
                seg_end = min(visible_end, row_end_day)
                seg_end_col = cell_row_col(seg_end)[1]

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

                if placed_track >= max_chips_per_cell - 1:
                    # No room: count as overflow contribution per covered cell.
                    for cc in range(col, seg_end_col + 1):
                        hidden_per_cell[(row, cc)] = hidden_per_cell.get((row, cc), 0) + 1
                else:
                    # Render the span chip.
                    cell = self._grid.cell_rect(d)
                    if cell is None:
                        d = seg_end + timedelta(days=1)
                        continue
                    x = col * CELL_W + 2
                    w = (seg_end_col - col + 1) * CELL_W - 4
                    y = HEADER_H + row * CELL_H + 22 + placed_track * (CHIP_H + CHIP_GAP)
                    chip = EventChip(
                        event,
                        QRectF(x, y, w, CHIP_H),
                        calendar_color=cal_color[inst.calendar_id],
                        mode=self._chip_mode,
                        show_time_prefix=False,
                        continues_left=(s_day < grid_start + timedelta(days=row * 7)),
                        continues_right=(e_day > row_end_day),
                    )
                    chip.edit_requested.connect(self._on_edit_requested)
                    chip.delete_requested.connect(self._on_delete_requested)
                    self._scene.addItem(chip)
                    self._chips.append(chip)

                    # Mark these cells as occupied at this track.
                    for cc in range(col, seg_end_col + 1):
                        per_row_track_occupied.setdefault((row, cc), []).append(
                            (placed_track, placed_track)
                        )

                d = seg_end + timedelta(days=1)

        # ── Layout pass 2: single-day events fill remaining tracks ───────
        for day, items in single_by_day.items():
            rc = cell_row_col(day)
            if rc is None:
                continue
            row, col = rc

            # Available tracks: those not occupied by multi-day spans in this cell.
            occupied_tracks = {
                t for (t, _t2) in per_row_track_occupied.get((row, col), [])
            }
            free_tracks = [t for t in range(max_chips_per_cell) if t not in occupied_tracks]

            items.sort(key=lambda x: (not x[1].all_day, x[0]))
            shown = 0
            for _i, (start_dt2, inst) in enumerate(items):
                if shown >= len(free_tracks):
                    break
                track = free_tracks[shown]
                shown += 1
                event = self._store.get_event(inst.uid, inst.calendar_id)
                if event is None:
                    continue
                if inst.calendar_id not in cal_color:
                    cal = self._store.get_calendar(inst.calendar_id)
                    cal_color[inst.calendar_id] = cal.color if cal else None
                x = col * CELL_W + 2
                y = HEADER_H + row * CELL_H + 22 + track * (CHIP_H + CHIP_GAP)
                time_prefix = None
                if not inst.all_day:
                    time_prefix = start_dt2.strftime("%H:%M")
                chip = EventChip(
                    event,
                    QRectF(x, y, CELL_W - 4, CHIP_H),
                    calendar_color=cal_color[inst.calendar_id],
                    mode=self._chip_mode,
                    show_time_prefix=not inst.all_day,
                    time_prefix=time_prefix,
                )
                chip.edit_requested.connect(self._on_edit_requested)
                chip.delete_requested.connect(self._on_delete_requested)
                self._scene.addItem(chip)
                self._chips.append(chip)

            hidden_singles = max(0, len(items) - shown)
            total_hidden = hidden_singles + hidden_per_cell.get((row, col), 0)
            if total_hidden > 0:
                # Bottom row of the cell: +N more.
                y = HEADER_H + row * CELL_H + CELL_H - CHIP_H - 1
                x = col * CELL_W + 2
                marker = _OverflowChip(
                    QRectF(x, y, CELL_W - 4, CHIP_H),
                    f"+{total_hidden} more",
                    day,
                    self._emit_day_activated,
                )
                self._scene.addItem(marker)
                self._chips.append(marker)

        # Also create +N markers for cells that have only multi-day overflow.
        for (row, col), n in hidden_per_cell.items():
            if any(
                isinstance(it, _OverflowChip)
                for it in self._chips
                if isinstance(it, _OverflowChip)
                and self._cell_of(it._rect) == (row, col)
            ):
                continue
            d = grid_start + timedelta(days=row * 7 + col)
            y = HEADER_H + row * CELL_H + CELL_H - CHIP_H - 1
            x = col * CELL_W + 2
            marker = _OverflowChip(
                QRectF(x, y, CELL_W - 4, CHIP_H),
                f"+{n} more",
                d,
                self._emit_day_activated,
            )
            self._scene.addItem(marker)
            self._chips.append(marker)

    def _cell_of(self, rect: QRectF) -> tuple[int, int]:
        col = int(rect.x() // CELL_W)
        row = int((rect.y() - HEADER_H) // CELL_H)
        return row, col

    def _emit_day_activated(self, d: date) -> None:
        self.day_activated.emit(d)

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
