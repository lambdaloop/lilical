from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QStackedWidget, QStatusBar, QToolBar, QVBoxLayout, QWidget

from lilical.ui.views.month import MonthView
from lilical.ui.views.week import WeekView


class MainWindow(QMainWindow):
    def __init__(
        self, *, config, event_store, sync_engine, recurrence, secrets
    ) -> None:
        super().__init__()
        self._cfg = config
        self._store = event_store
        self._sync = sync_engine

        self.setWindowTitle("lilical")
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._toolbar = QToolBar()
        self.addToolBar(self._toolbar)
        self._toolbar.addWidget(QLabel("lilical"))

        self._stack = QStackedWidget()
        self._month_view = MonthView(event_store)
        self._week_view = WeekView(event_store)
        self._stack.addWidget(self._month_view)
        self._stack.addWidget(self._week_view)
        self._stack.setCurrentWidget(self._month_view)
        layout.addWidget(self._stack)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        self._store.events_changed.connect(self._on_events_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_finished.connect(self._on_sync_finished)

    def _on_events_changed(self, calendar_id: str, uids: set) -> None:
        pass

    def _on_sync_started(self, account_id: str) -> None:
        self._status.showMessage(f"Syncing {account_id}...")

    def _on_sync_finished(self, account_id: str, n_changes: int) -> None:
        self._status.showMessage(f"Synced {account_id} ({n_changes} changes)", 5000)

    def closeEvent(self, e):
        super().closeEvent(e)
