from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QSizePolicy,
)

from lilical.ui import theme

_DOW_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_HEADER_H = 20
_CELL_H = 24
_VIEWPORT_PADDING = 4  # QGraphicsView frame border on each axis


class MiniMonthGrid(QGraphicsView):
    """A compact month calendar used in the sidebar for date navigation."""

    selected = Signal(date)
    month_changed = Signal(int, int)  # year, month

    def __init__(self, year: int | None = None, month: int | None = None) -> None:
        super().__init__()
        today = date.today()
        self._year = year or today.year
        self._month = month or today.month
        self._selected = today
        self._active_start: date | None = None
        self._active_end: date | None = None
        self._cell_w = 26  # updated in resizeEvent
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_HEADER_H + 6 * _CELL_H + _VIEWPORT_PADDING)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._day_rects: dict[date, tuple[float, float, float, float]] = {}
        self._render()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._cell_w = max(20, self.width() // 7)
        self._render()

    def set_month(self, year: int, month: int) -> None:
        self._year = year
        self._month = month
        self._render()

    def set_selected(self, d: date) -> None:
        self._selected = d
        self._render()

    def set_active_range(self, start: date, end: date) -> None:
        if end < start:
            start, end = end, start
        self._active_start = start
        self._active_end = end
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
        cw = self._cell_w
        ch = _CELL_H
        first = date(self._year, self._month, 1)
        start = first - timedelta(days=first.weekday())
        today = date.today()
        a_start = self._active_start
        a_end = self._active_end

        # Day-of-week header row
        dow_font = QFont("sans-serif", 7)
        for i, label in enumerate(_DOW_LABELS):
            item = self._scene.addText(label, dow_font)
            item.setDefaultTextColor(QColor(theme.TEXT_DISABLED))
            item.setPos(i * cw + 3, 2)

        for d in range(42):
            x = (d % 7) * cw
            y = _HEADER_H + (d // 7) * ch
            cur = start + timedelta(days=d)
            in_month = cur.month == self._month
            if not in_month:
                continue

            self._day_rects[cur] = (x, y, cw, ch)

            in_range = (
                a_start is not None and a_end is not None and a_start <= cur <= a_end
            )
            if in_range:
                band_color = QColor(theme.ACCENT_FILL)
                band_color.setAlpha(70)
                self._scene.addRect(
                    x, y, cw - 1, ch - 1, QPen(Qt.PenStyle.NoPen), band_color
                )

            if cur == today:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor(theme.ACCENT)))
            if cur == self._selected and not in_range:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor(theme.ACCENT_FILL)))

            item = self._scene.addText(str(cur.day), QFont("sans-serif", 8))
            item.setPos(x + max(3, (cw - 16) // 2), y + 3)
            if cur == today:
                item.setDefaultTextColor(QColor(theme.ACCENT))
            elif in_range:
                item.setDefaultTextColor(QColor(theme.TEXT_PRIMARY))
            else:
                item.setDefaultTextColor(QColor(theme.TEXT_SECONDARY))

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
