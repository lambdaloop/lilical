from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore


class Sidebar(QWidget):
    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        self.setFixedWidth(240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("Calendars")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self._cal_layout = QVBoxLayout(scroll_widget)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        add_btn = QPushButton("+ Add account")
        layout.addWidget(add_btn)

        self._checkboxes: dict[str, QCheckBox] = {}
        self._refresh()

    def _refresh(self) -> None:
        for acc in self._store.list_accounts():
            for cal in self._store.list_calendars(acc.id):
                if cal.id not in self._checkboxes:
                    cb = QCheckBox(cal.display_name)
                    cb.setChecked(cal.is_visible)
                    self._cal_layout.addWidget(cb)
                    self._checkboxes[cal.id] = cb
