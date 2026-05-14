from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class PreferencesDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current_theme: str = "dark",
        current_week_start: str = "monday",
        current_default_view: str = "Month",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["dark", "light"])
        self._theme_combo.setCurrentText(current_theme)
        form.addRow("Theme:", self._theme_combo)

        self._week_start_combo = QComboBox()
        self._week_start_combo.addItems(["monday", "sunday", "saturday"])
        self._week_start_combo.setCurrentText(current_week_start)
        form.addRow("Week starts on:", self._week_start_combo)

        self._default_view_combo = QComboBox()
        self._default_view_combo.addItems(["Month", "Week", "Day", "Agenda"])
        self._default_view_combo.setCurrentText(current_default_view)
        form.addRow("Default view:", self._default_view_combo)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def theme(self) -> str:
        return self._theme_combo.currentText()

    @property
    def week_start(self) -> str:
        return self._week_start_combo.currentText()

    @property
    def default_view(self) -> str:
        return self._default_view_combo.currentText()
