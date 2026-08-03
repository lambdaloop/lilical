from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QTextOption
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QSizePolicy,
)

from lilical.ui import theme
from lilical.ui.views._week_start import dow_labels_short, start_of_week
from lilical.utils.timezone import display_today

_BASE_HEADER_H = 20
_BASE_CELL_H = 24
_VIEWPORT_PADDING = 4  # QGraphicsView frame border on each axis

_HEADER_H = _BASE_HEADER_H
_CELL_H = _BASE_CELL_H


def apply_scale(factor: float) -> None:
    g = globals()
    g["_HEADER_H"] = max(1, round(_BASE_HEADER_H * factor))
    g["_CELL_H"] = max(1, round(_BASE_CELL_H * factor))


class MiniMonthGrid(QGraphicsView):
    """A compact month calendar used in the sidebar for date navigation."""

    selected = Signal(date)
    month_changed = Signal(int, int)  # year, month

    def __init__(self, year: int | None = None, month: int | None = None) -> None:
        super().__init__()
        today = display_today()
        self.year = year or today.year
        self.month = month or today.month
        self._selected = today
        self._week_start = "monday"
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
        self.render()

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802
        super().resizeEvent(event)
        self._cell_w = max(20, self.width() // 7)
        self.render()

    def set_month(self, year: int, month: int) -> None:
        self.year = year
        self.month = month
        self.render()

    def set_selected(self, d: date) -> None:
        self._selected = d
        self.render()

    def reset_scale(self) -> None:
        """Re-apply fixed height after a global scale change, then re-render."""
        self.setFixedHeight(_HEADER_H + 6 * _CELL_H + _VIEWPORT_PADDING)
        self.render()

    def set_active_range(self, start: date, end: date) -> None:
        if end < start:
            start, end = end, start
        self._active_start = start
        self._active_end = end
        if start.year != self.year or start.month != self.month:
            self.year = start.year
            self.month = start.month
        self.render()

    def clear_active_range(self) -> None:
        self._active_start = None
        self._active_end = None
        self.render()

    def render(self) -> None:  # type: ignore[reportIncompatibleMethodOverride]
        self._scene.clear()
        self._day_rects = {}
        cw = self._cell_w
        ch = _CELL_H
        first = date(self.year, self.month, 1)
        start = start_of_week(first, self._week_start)
        today = display_today()
        a_start = self._active_start
        a_end = self._active_end

        # Day-of-week header row
        dow_font = QFont(theme.FONT_FAMILY, 8)
        for i, label in enumerate(dow_labels_short(self._week_start)):
            item = self._scene.addText(label, dow_font)
            item.setDefaultTextColor(QColor(theme.TEXT_DISABLED))
            item.setPos(i * cw + 3, 2)

        for d in range(42):
            x = (d % 7) * cw
            y = _HEADER_H + (d // 7) * ch
            cur = start + timedelta(days=d)
            in_month = cur.month == self.month
            if not in_month:
                continue

            self._day_rects[cur] = (x, y, cw, ch)

            in_range = (
                a_start is not None and a_end is not None and a_start <= cur <= a_end
            )
            if in_range:
                band_color = QColor(theme.ACCENT_FILL)
                band_color.setAlpha(55)
                self._scene.addRect(
                    x, y, cw - 1, ch - 1, QPen(Qt.PenStyle.NoPen), band_color
                )

            if cur == today:
                pill = QPainterPath()
                pill.addRoundedRect(x + 1, y + 1, cw - 3, ch - 3, 4, 4)
                self._scene.addPath(
                    pill, QPen(Qt.PenStyle.NoPen), QColor(theme.ACCENT_FILL)
                )
            elif cur == self._selected and not in_range:
                ring = QPainterPath()
                ring.addRoundedRect(x + 1, y + 1, cw - 3, ch - 3, 4, 4)
                ring_pen = QPen(QColor(theme.ACCENT))
                ring_pen.setWidth(2)
                self._scene.addPath(ring, ring_pen, QColor(Qt.GlobalColor.transparent))

            item = self._scene.addText(str(cur.day), QFont(theme.FONT_FAMILY, 9))
            item.setTextWidth(cw)
            item.document().setDefaultTextOption(
                QTextOption(Qt.AlignmentFlag.AlignHCenter)
            )
            item.setPos(x, y + (ch - item.boundingRect().height()) / 2)
            if cur == today or in_range:
                item.setDefaultTextColor(QColor(theme.TEXT_PRIMARY))
            else:
                item.setDefaultTextColor(QColor(theme.TEXT_SECONDARY))

    def set_week_start(self, week_start: str) -> None:
        if week_start == self._week_start:
            return
        self._week_start = week_start
        self.render()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        scene_pos: QPointF = self.mapToScene(event.pos())
        sx, sy = scene_pos.x(), scene_pos.y()
        for d, (x, y, w, h) in self._day_rects.items():
            if x <= sx < x + w and y <= sy < y + h:
                self._selected = d
                self.render()
                self.selected.emit(d)
                return
        super().mousePressEvent(event)
