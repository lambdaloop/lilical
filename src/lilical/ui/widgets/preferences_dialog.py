from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QWidget,
)

_SNAP_OPTIONS: list[tuple[str, int]] = [
    ("5 min", 5),
    ("10 min", 10),
    ("15 min", 15),
    ("30 min", 30),
    ("60 min", 60),
]

_CHIP_MODE_OPTIONS: list[tuple[str, str]] = [
    ("Bars", "bars"),
    ("Text", "text"),
]

_TIME_FORMAT_OPTIONS: list[tuple[str, str]] = [
    ("24h  (14:30)", "24h"),
    ("12h  (2:30 PM)", "12h"),
]


class PreferencesDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current_theme: str = "dark",
        current_week_start: str = "monday",
        current_default_view: str = "Month",
        current_snap_minutes: int = 15,
        current_chip_mode: str = "bars",
        current_time_format: str = "24h",
        current_enable_completed_events: bool = False,
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

        self._snap_combo = QComboBox()
        for label, _ in _SNAP_OPTIONS:
            self._snap_combo.addItem(label)
        snap_idx = next(
            (i for i, (_, v) in enumerate(_SNAP_OPTIONS) if v == current_snap_minutes),
            2,
        )
        self._snap_combo.setCurrentIndex(snap_idx)
        form.addRow("Drag snap interval:", self._snap_combo)

        self._chip_mode_combo = QComboBox()
        for label, _ in _CHIP_MODE_OPTIONS:
            self._chip_mode_combo.addItem(label)
        chip_idx = next(
            (
                i
                for i, (_, v) in enumerate(_CHIP_MODE_OPTIONS)
                if v == current_chip_mode
            ),
            0,
        )
        self._chip_mode_combo.setCurrentIndex(chip_idx)
        form.addRow("Chip style:", self._chip_mode_combo)

        self._time_format_combo = QComboBox()
        for label, _ in _TIME_FORMAT_OPTIONS:
            self._time_format_combo.addItem(label)
        tf_idx = next(
            (
                i
                for i, (_, v) in enumerate(_TIME_FORMAT_OPTIONS)
                if v == current_time_format
            ),
            0,
        )
        self._time_format_combo.setCurrentIndex(tf_idx)
        form.addRow("Time format:", self._time_format_combo)

        self._enable_completed_check = QCheckBox()
        self._enable_completed_check.setChecked(current_enable_completed_events)
        form.addRow("Enable completed events:", self._enable_completed_check)

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

    @property
    def snap_minutes(self) -> int:
        idx = self._snap_combo.currentIndex()
        if 0 <= idx < len(_SNAP_OPTIONS):
            return _SNAP_OPTIONS[idx][1]
        return 15

    @property
    def chip_mode(self) -> str:
        idx = self._chip_mode_combo.currentIndex()
        if 0 <= idx < len(_CHIP_MODE_OPTIONS):
            return _CHIP_MODE_OPTIONS[idx][1]
        return "bars"

    @property
    def time_format(self) -> str:
        idx = self._time_format_combo.currentIndex()
        if 0 <= idx < len(_TIME_FORMAT_OPTIONS):
            return _TIME_FORMAT_OPTIONS[idx][1]
        return "24h"

    @property
    def enable_completed_events(self) -> bool:
        return self._enable_completed_check.isChecked()
