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
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFixedSize(200, 180)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._render()
        # Map of pixel position → date for hit-testing clicks
        self._day_rects: dict[date, tuple[float, float, float, float]] = {}

    def set_month(self, year: int, month: int) -> None:
        self._year = year
        self._month = month
        self._render()

    def set_selected(self, d: date) -> None:
        self._selected = d
        self._render()

    def _render(self) -> None:
        self._scene.clear()
        self._day_rects = {}
        first = date(self._year, self._month, 1)
        start = first - timedelta(days=first.weekday())
        cw = self._CELL_W
        ch = self._CELL_H
        today = date.today()

        for d in range(42):
            x = (d % 7) * cw
            y = 20 + (d // 7) * ch
            cur = start + timedelta(days=d)
            in_month = cur.month == self._month
            if not in_month:
                continue

            self._day_rects[cur] = (x, y, cw, ch)

            if cur == self._selected:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor("#3b82f6")))
            elif cur == today:
                self._scene.addRect(x, y, cw - 1, ch - 1, QPen(QColor("#9ec5ff")))

            item = self._scene.addText(str(cur.day), QFont("sans-serif", 8))
            item.setPos(x + 6, y + 2)
            item.setDefaultTextColor(
                QColor("#c8c8c8") if cur != today else QColor("#9ec5ff")
            )

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
