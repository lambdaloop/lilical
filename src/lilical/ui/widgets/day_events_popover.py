"""Transient hover popup listing all events for one calendar day."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.ui.widgets._popover_rows import PopoverEvent, make_row

__all__ = ["DayEventsPopover", "PopoverEvent"]


class DayEventsPopover(QFrame):
    """Frameless non-focus-stealing window shown when hovering a dense day cell."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 8)
        outer.setSpacing(2)

        self._header = QLabel()
        outer.addWidget(self._header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 2, 0, 0)
        self._rows_layout.setSpacing(1)
        outer.addWidget(self._rows_widget)

        self._current_rows: list[QWidget] = []

    def show_for_day(
        self,
        day: date,
        events: list[PopoverEvent],
        global_pos: QPoint,
    ) -> None:
        self._apply_theme()

        for row in self._current_rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._current_rows.clear()

        self._header.setText(day.strftime("%A, %B %-d"))

        for ev in events:
            row = make_row(ev)
            self._rows_layout.addWidget(row)
            self._current_rows.append(row)

        self.adjustSize()
        self._position_near(global_pos)
        self.show()

    def _position_near(self, cursor_global: QPoint) -> None:
        screen = self.screen()
        avail = screen.availableGeometry() if screen else None
        x = cursor_global.x() + 16
        y = cursor_global.y() - 16
        if avail:
            if x + self.width() > avail.right():
                x = cursor_global.x() - self.width() - 16
            if y + self.height() > avail.bottom():
                y = avail.bottom() - self.height()
            if y < avail.top():
                y = avail.top()
        self.move(x, y)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"DayEventsPopover {{"
            f" background: {theme.BG_SURFACE_3};"
            f" border: 1px solid {theme.BORDER_STRONG};"
            f" border-radius: 5px;"
            f"}}"
            f" QLabel {{ background: transparent; }}"
            f" QFrame {{ background: {theme.BORDER}; }}"
        )
        self._header.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-family: {theme.FONT_FAMILY};"
            f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
            f" font-weight: bold;"
        )
