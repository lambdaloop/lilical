from __future__ import annotations

from typing import override

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from lilical.ui.sidebar import Sidebar
from lilical.ui.tray import SystemTray
from lilical.ui.views.agenda import AgendaView
from lilical.ui.views.day import DayView
from lilical.ui.views.month import MonthView
from lilical.ui.views.week import WeekView
from lilical.ui.views.year import YearView


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
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Sidebar
        self._sidebar = Sidebar(event_store)
        main_layout.addWidget(self._sidebar)

        # Right side: toolbar + stacked views
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._toolbar = QToolBar()
        right_layout.addWidget(self._toolbar)

        views = [
            ("Month", MonthView(event_store)),
            ("Week", WeekView(event_store)),
            ("Day", DayView(event_store)),
            ("Year", YearView(event_store)),
            ("Agenda", AgendaView(event_store)),
        ]
        self._stack = QStackedWidget()
        for name, view in views:
            self._stack.addWidget(view)
            act = QAction(name, self)
            act.setCheckable(True)
            act.triggered.connect(lambda _, v=view: self._stack.setCurrentWidget(v))
            self._toolbar.addAction(act)

        right_layout.addWidget(self._stack)
        main_layout.addWidget(right)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        # System tray
        self._tray = SystemTray(self)
        self._tray.show()

        # Dark theme default
        self._apply_theme("dark")

        self._store.events_changed.connect(self._on_events_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_finished.connect(self._on_sync_finished)

    def _apply_theme(self, name: str) -> None:
        try:
            from pathlib import Path
            theme_path = Path(__file__).parent / "styles" / f"{name}.qss"
            if theme_path.exists():
                with open(theme_path) as f:
                    self.setStyleSheet(f.read())
        except Exception:
            pass

    def _on_events_changed(self, calendar_id: str, uids: set) -> None:
        pass

    def _on_sync_started(self, account_id: str) -> None:
        self._status.showMessage(f"Syncing {account_id}...")

    def _on_sync_finished(self, account_id: str, n_changes: int) -> None:
        self._status.showMessage(f"Synced {account_id} ({n_changes} changes)", 5000)

    @override
    def closeEvent(self, e):
        super().closeEvent(e)
