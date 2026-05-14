from __future__ import annotations

from datetime import date, timedelta
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore

MONTHS_PER_ROW = 4
CELL_SIZE = 16
HEADER_H = 20
PAD = 20


class YearView(QGraphicsView):
    def __init__(self, store: EventStore, year: int | None = None) -> None:
        super().__init__()
        self._store = store
        self._year = year or date.today().year
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @override
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        w = self.viewport().width()
        cols = MONTHS_PER_ROW
        rows = 12 // cols
        mw = (w - PAD * 2) // cols
        mh = rows * 7 * CELL_SIZE + HEADER_H * 2

        painter.setPen(QColor("#e8e8e8"))
        painter.setFont(QFont("sans-serif", 14, QFont.Weight.Bold))
        painter.drawText(
            QRectF(0, 0, w, 40),
            Qt.AlignmentFlag.AlignCenter,
            str(self._year),
        )

        painter.setFont(QFont("sans-serif", 8))
        for m in range(12):
            row = m // cols
            col = m % cols
            ox = PAD + col * mw
            oy = 50 + row * mh
            month_name = date(self._year, m + 1, 1).strftime("%B")
            painter.drawText(
                QRectF(ox, oy, mw, HEADER_H),
                Qt.AlignmentFlag.AlignCenter,
                month_name,
            )

            first = date(self._year, m + 1, 1)
            start = first - timedelta(days=first.weekday())
            for d in range(42):
                cx = ox + (d % 7) * CELL_SIZE
                cy = oy + HEADER_H + (d // 7) * CELL_SIZE
                cur = start + timedelta(days=d)
                if cur.month == m + 1:
                    painter.setPen(QColor("#e8e8e8"))
                    painter.drawText(cx + 2, cy + 12, str(cur.day))
