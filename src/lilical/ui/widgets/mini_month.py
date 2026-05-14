from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy, QVBoxLayout, QWidget


class MiniMonthGrid(QGraphicsView):
    selected = Signal(date)

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
        self._render()

    def _render(self) -> None:
        self._scene.clear()
        first = date(self._year, self._month, 1)
        start = first - timedelta(days=first.weekday())
        cell_w = 26
        cell_h = 20

        for d in range(42):
            x = (d % 7) * cell_w
            y = 20 + (d // 7) * cell_h
            cur = start + timedelta(days=d)
            in_month = cur.month == self._month
            if not in_month:
                continue
            if cur == self._selected:
                self._scene.addRect(x, y, cell_w, cell_h, QPen(QColor("#2563eb")))
            item = self._scene.addText(str(cur.day), QFont("sans-serif", 8))
            item.setPos(x + 6, y + 2)
            item.setDefaultColor(QColor("#e8e8e8"))
