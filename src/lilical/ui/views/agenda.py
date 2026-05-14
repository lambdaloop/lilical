from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore


class AgendaView(QWidget):
    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["", "Time", "Event"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().hide()
        layout.addWidget(self._table)

        self._populate()

    def _populate(self) -> None:
        start = date.today()
        rows = 0
        for d_offset in range(14):
            d = start + timedelta(days=d_offset)
            rows += 3
        self._table.setRowCount(rows)
        row = 0
        for d_offset in range(14):
            d = start + timedelta(days=d_offset)
            item = QTableWidgetItem(d.strftime("%a, %b %d"))
            font = QFont()
            font.setBold(True)
            item.setFont(font)
            self._table.setItem(row, 0, item)
            self._table.setItem(row, 1, QTableWidgetItem(""))
            self._table.setItem(row, 2, QTableWidgetItem(""))
            row += 1
            for _ in range(2):
                self._table.setItem(row, 0, QTableWidgetItem(""))
                self._table.setItem(row, 1, QTableWidgetItem(""))
                self._table.setItem(row, 2, QTableWidgetItem(""))
                row += 1
