from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QSizePolicy

from lilical.storage.event_store import EventStore

MONTHS_PER_ROW = 4
CELL_SIZE = 16
HEADER_H = 20
MONTH_LABEL_H = 22
PAD = 20
TOP_MARGIN = 50
GAP = 4


def _month_grid_size() -> tuple[float, float]:
    mw = 7 * CELL_SIZE
    mh = MONTH_LABEL_H + 6 * CELL_SIZE
    return mw, mh


def _total_size() -> tuple[float, float]:
    cols = MONTHS_PER_ROW
    rows = 12 // cols
    mw, mh = _month_grid_size()
    tw = PAD * 2 + cols * mw + (cols - 1) * GAP
    th = TOP_MARGIN + rows * mh + (rows - 1) * GAP + PAD
    return tw, th


def _density_color(count: int) -> QColor:
    if count == 0:
        return QColor("#262626")
    if count <= 3:
        return QColor("#1a3a5c")
    if count <= 6:
        return QColor("#2a5a8c")
    return QColor("#3a7acc")


class YearGrid(QGraphicsItem):
    def __init__(self, year: int, densities: dict[tuple[int, int], int]) -> None:
        super().__init__()
        self._year = year
        self._densities = densities
        self._today = date.today()

    @override
    def boundingRect(self) -> QRectF:
        tw, th = _total_size()
        return QRectF(0, 0, tw, th)

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:
        tw, _ = _total_size()
        painter.setPen(QColor("#e8e8e8"))
        painter.setFont(QFont("sans-serif", 14, QFont.Weight.Bold))
        painter.drawText(
            QRectF(0, 0, tw, 40),
            Qt.AlignmentFlag.AlignCenter,
            str(self._year),
        )

        painter.setFont(QFont("sans-serif", 8))
        mw, mh = _month_grid_size()
        for m in range(12):
            row = m // MONTHS_PER_ROW
            col = m % MONTHS_PER_ROW
            ox = PAD + col * (mw + GAP)
            oy = TOP_MARGIN + row * (mh + GAP)
            month_name = date(self._year, m + 1, 1).strftime("%B")
            painter.drawText(
                QRectF(ox, oy, mw, MONTH_LABEL_H),
                Qt.AlignmentFlag.AlignCenter,
                month_name,
            )

            first = date(self._year, m + 1, 1)
            start = first - timedelta(days=first.weekday())
            for d in range(42):
                cx = ox + (d % 7) * CELL_SIZE
                cy = oy + MONTH_LABEL_H + (d // 7) * CELL_SIZE
                cur = start + timedelta(days=d)
                if cur.month == m + 1:
                    count = self._densities.get((m, cur.day), 0)
                    painter.setBrush(_density_color(count))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawRect(cx, cy, CELL_SIZE, CELL_SIZE)

                    if cur == self._today:
                        painter.setPen(QPen(QColor("#5e9fff"), 2))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawRect(cx + 1, cy + 1, CELL_SIZE - 2, CELL_SIZE - 2)

                    painter.setPen(QColor("#e8e8e8"))
                    painter.drawText(cx + 2, cy + 12, str(cur.day))


class YearView(QGraphicsView):
    def __init__(self, store: EventStore, year: int | None = None) -> None:
        super().__init__()
        self._store = store
        self._year = year or date.today().year
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._grid = YearGrid(self._year, {})
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
        self.refresh()

    def refresh(self) -> None:
        densities: dict[tuple[int, int], int] = {}
        start_dt = datetime(self._year, 1, 1, tzinfo=timezone.utc)
        end_dt = datetime(self._year + 1, 1, 1, tzinfo=timezone.utc)
        for inst in self._store.list_instances(start_dt, end_dt):
            try:
                t = datetime.fromisoformat(inst.dtstart_local)
            except (ValueError, TypeError):
                continue
            if t.year != self._year:
                continue
            key = (t.month - 1, t.day)
            densities[key] = densities.get(key, 0) + 1
        self._scene.removeItem(self._grid)
        self._grid = YearGrid(self._year, densities)
        self._scene.addItem(self._grid)
        self._scene.setSceneRect(self._grid.boundingRect())
