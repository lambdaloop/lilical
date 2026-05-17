from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon


class SystemTray(QSystemTrayIcon):
    def __init__(self, main_window: QMainWindow) -> None:
        super().__init__(main_window)
        self._main_window = main_window

        icon = QIcon.fromTheme("org.lilical.Lilical")
        if icon.isNull():
            icon = QIcon.fromTheme("x-office-calendar")
        if icon.isNull():
            icon = QIcon.fromTheme("calendar")
        if icon.isNull():
            icon = QIcon.fromTheme("office-calendar")
        self.setIcon(icon)
        self.setToolTip("lilical")

        menu = QMenu()

        show_act = QAction("Show", self)
        show_act.triggered.connect(self._show_window)
        menu.addAction(show_act)

        menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _show_window(self) -> None:
        self._main_window.showNormal()
        self._main_window.raise_()
        self._main_window.activateWindow()

    def _on_activated(self, reason: int) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()
