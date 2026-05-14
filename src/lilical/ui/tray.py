from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


class SystemTray(QSystemTrayIcon):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(QIcon.fromTheme("x-office-calendar"))
        self.setToolTip("lilical")

        menu = QMenu()
        show_act = QAction("Show", self)
        show_act.triggered.connect(self._show_window)
        menu.addAction(show_act)

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _show_window(self) -> None:
        for w in QApplication.topLevelWidgets():
            w.show()
            w.raise_()
            break

    def _on_activated(self, reason: int) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()
