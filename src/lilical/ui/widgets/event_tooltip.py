"""Lightweight frameless tooltip shown near the cursor when the inspector is hidden."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.ui.widgets._popover_rows import PopoverEvent, make_row


class EventTooltip(QFrame):
    """Frameless non-focus-stealing tooltip for hovered events / clusters."""

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
        outer.setSpacing(4)

        # ── Event section ──────────────────────────────────────────────
        self._time = QLabel()
        outer.addWidget(self._time)

        title_row = QWidget()
        tr_layout = QHBoxLayout(title_row)
        tr_layout.setContentsMargins(0, 0, 0, 0)
        tr_layout.setSpacing(6)
        self._swatch = QFrame()
        self._swatch.setFixedSize(8, 8)
        tr_layout.addWidget(self._swatch, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setMaximumWidth(260)
        tr_layout.addWidget(self._title, 1)
        outer.addWidget(title_row)

        # ── Cluster section ────────────────────────────────────────────
        self._sep = QFrame()
        self._sep.setFrameShape(QFrame.Shape.HLine)
        self._sep.setVisible(False)
        outer.addWidget(self._sep)

        self._cluster_header = QLabel()
        self._cluster_header.setVisible(False)
        outer.addWidget(self._cluster_header)

        self._cluster_rows_widget = QWidget()
        self._cluster_rows_layout = QVBoxLayout(self._cluster_rows_widget)
        self._cluster_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._cluster_rows_layout.setSpacing(2)
        self._cluster_rows_widget.setVisible(False)
        outer.addWidget(self._cluster_rows_widget)

        self._current_rows: list[QWidget] = []
        self._apply_theme()

    def show_event(
        self, ev: PopoverEvent, _notes: str | None, global_pos: QPoint
    ) -> None:
        self._apply_theme()
        self._clear_cluster_rows()
        self._sep.setVisible(False)
        self._cluster_header.setVisible(False)
        self._cluster_rows_widget.setVisible(False)

        self._time.setText(ev.time_str)
        self._time.setVisible(True)

        self._title.setText(ev.title or "(no title)")
        color = ev.calendar_color or theme.CHIP_FALLBACK
        self._swatch.setStyleSheet(
            f"background: {color}; border-radius: 4px;"
        )

        self.adjustSize()
        self._position_near(global_pos)
        self.show()

    def show_cluster(
        self,
        primary: PopoverEvent,
        siblings: list[PopoverEvent],
        global_pos: QPoint,
    ) -> None:
        self._apply_theme()
        self._clear_cluster_rows()

        self._time.setText(primary.time_str)
        self._time.setVisible(True)

        self._title.setText(primary.title or "(no title)")
        color = primary.calendar_color or theme.CHIP_FALLBACK
        self._swatch.setStyleSheet(
            f"background: {color}; border-radius: 4px;"
        )

        self._sep.setVisible(True)

        n = len(siblings)
        first_time = siblings[0].time_str.split("–")[0].strip()
        last_time = siblings[-1].time_str.split("–")[-1].strip()
        suffix = "EVENT" if n == 1 else "EVENTS"
        self._cluster_header.setText(f"{first_time} — {last_time} · {n} {suffix}")
        self._cluster_header.setVisible(True)

        for sib in siblings:
            row = make_row(sib)
            self._cluster_rows_layout.addWidget(row)
            self._current_rows.append(row)
        self._cluster_rows_widget.setVisible(True)

        self.adjustSize()
        self._position_near(global_pos)
        self.show()

    def hide_tooltip(self) -> None:
        self.hide()

    def _clear_cluster_rows(self) -> None:
        for row in self._current_rows:
            self._cluster_rows_layout.removeWidget(row)
            row.deleteLater()
        self._current_rows.clear()

    def _position_near(self, cursor_global: QPoint) -> None:
        screen = self.screen()
        avail = screen.availableGeometry() if screen else None
        x = cursor_global.x() + 16
        y = cursor_global.y() - 8
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
            f"EventTooltip {{"
            f" background: {theme.BG_SURFACE_3};"
            f" border: 1px solid {theme.BORDER_STRONG};"
            f" border-radius: 5px;"
            f"}}"
            f" QLabel {{ background: transparent; }}"
        )
        self._time.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
        )
        self._title.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY};"
            f" font-size: {theme.FONT_CHIP_TITLE}pt;"
            f" font-weight: bold;"
        )
        self._cluster_header.setStyleSheet(
            f"color: {theme.TEXT_DISABLED};"
            f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
            f" letter-spacing: 1px;"
            f" font-weight: bold;"
        )
        self._sep.setStyleSheet(f"background: {theme.BORDER};")
