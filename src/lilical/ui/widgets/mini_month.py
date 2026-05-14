from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
)


class MiniMonthGrid(QGraphicsView):
    """A compact month calendar used in the sidebar for date navigation."""

    selected = Signal(date)
    month_changed = Signal(int, int)  # year, month

    _CELL_W = 26
    _CELL_H = 20

    def __init__(self, year: int | None = None, month: int | None = None) -> None:
        super().__init__()
        today = date.today()
        self._year = year or today.year
        self._month = month or today.month
        self._selected = today
        # Active range of days highlighted to mirror the current view. For
        # the Day view this is a single date; for the Week view it spans the
        # visible week(s). When None, no band is drawn.
        self._active_start: date | None = None
        self._active_end: date | None = None
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFixedSize(200, 180)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._day_rects: dict[date, tuple[float, float, float, float]] = {}
        self._render()

    def set_month(self, year: int, month: int) -> None:
        self._year = year
        self._month = month
        self._render()

    def set_selected(self, d: date) -> None:
        self._selected = d
        self._render()

    def set_active_range(self, start: date, end: date) -> None:
        """Highlight the inclusive ``[start, end]`` date band and, if it falls
        outside the currently-displayed month, flip the grid to the month
        containing ``start`` so the band is visible."""
        if end < start:
            start, end = end, start
        self._active_start = start
        self._active_end = end
        # Auto-flip to the month containing the start of the range. If the
        # range crosses a month boundary, we still anchor on `start`'s month
        # — partial highlight on the visible cells is the right look.
        if start.year != self._year or start.month != self._month:
            self._year = start.year
            self._month = start.month
        self._render()

    def clear_active_range(self) -> None:
        self._active_start = None
        self._active_end = None
        self._render()

    def _render(self) -> None:
        self._scene.clear()
        self._day_rects = {}
        first = date(self._year, self._month, 1)
        start = first - timedelta(days=first.weekday())
        cw = self._CELL_W
        ch = self._CELL_H
        today = date.today()
        a_start = self._active_start
        a_end = self._active_end

        for d in range(42):
            x = (d % 7) * cw
            y = 20 + (d // 7) * ch
            cur = start + timedelta(days=d)
            in_month = cur.month == self._month
            if not in_month:
                continue

            self._day_rects[cur] = (x, y, cw, ch)

            # Active-range band: filled background so a multi-day span reads
            # as a continuous strip.
            in_range = a_start is not None and a_end is not None and a_start <= cur <= a_end
            if in_range:
                band_color = QColor("#3b82f6")
                band_color.setAlpha(70)
                self._scene.addRect(
                    x, y, cw - 1, ch - 1, QPen(Qt.PenStyle.NoPen), band_color
                )

            # Today gets a ring outline, regardless of range membership.
            if cur == today:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor("#9ec5ff")))
            # Single-day "selected" outline (matches Day-view convention).
            if cur == self._selected and not in_range:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor("#3b82f6")))

            item = self._scene.addText(str(cur.day), QFont("sans-serif", 8))
            item.setPos(x + 6, y + 2)
            if cur == today:
                item.setDefaultTextColor(QColor("#9ec5ff"))
            elif in_range:
                item.setDefaultTextColor(QColor("#ffffff"))
            else:
                item.setDefaultTextColor(QColor("#c8c8c8"))

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        scene_pos: QPointF = self.mapToScene(event.pos())
        sx, sy = scene_pos.x(), scene_pos.y()
        for d, (x, y, w, h) in self._day_rects.items():
            if x <= sx < x + w and y <= sy < y + h:
                self._selected = d
                self._render()
                self.selected.emit(d)
                return
        super().mousePressEvent(event)
