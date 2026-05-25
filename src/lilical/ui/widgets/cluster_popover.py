"""Side-anchored agenda popover for dense-overlap event clusters."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.ui.widgets._popover_rows import PopoverEvent, make_row


class ClusterPopover(QFrame):
    """Frameless side-anchored popup listing all events in an overlap cluster.

    Appears to the right of the day column (auto-flips left when near the
    view's right edge). Has a 150 ms re-entry tolerance: mouse leaving the
    cluster starts a hide timer that is cancelled if the mouse enters this
    widget before it fires.
    """

    event_activated = Signal(str)  # emits event uid when a row is clicked

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
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(150)
        self._hide_timer.timeout.connect(self.hide)

    def show_for_cluster(
        self,
        events: list[PopoverEvent],
        anchor_global_topleft: QPoint,
        column_right_global: int,
        view_right_edge_global: int,
    ) -> None:
        """Display the popover anchored to a cluster's position.

        Args:
            events: ordered list of events to show (dominant first or
                sorted by start time — caller decides).
            anchor_global_topleft: top-left corner of the cluster rect in
                global screen coordinates (used to align the popover top edge).
            column_right_global: right edge of the day column in global coords.
            view_right_edge_global: right edge of the whole week/day view in
                global coords (used for the auto-flip decision).
        """
        self._hide_timer.stop()
        self._apply_theme()

        for row in self._current_rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._current_rows.clear()

        n = len(events)
        if n > 0:
            # Build a time-range header from first-start to last-end.
            first_time = events[0].time_str.split("–")[0].strip()
            last_time = events[-1].time_str.split("–")[-1].strip()
            suffix = "EVENT" if n == 1 else "EVENTS"
            self._header.setText(f"{first_time} — {last_time} · {n} {suffix}")

        for ev in events:
            row_widget = make_row(ev)
            if ev.uid:
                uid = ev.uid
                row_widget.mousePressEvent = (  # type: ignore[method-assign]
                    lambda _e, u=uid: self.event_activated.emit(u)
                )
                row_widget.setCursor(Qt.CursorShape.PointingHandCursor)
            self._rows_layout.addWidget(row_widget)
            self._current_rows.append(row_widget)

        self.adjustSize()
        pos = self._position_side_anchored(
            anchor_global_topleft, column_right_global, view_right_edge_global
        )
        self.move(pos)
        self.show()

    def _position_side_anchored(
        self,
        anchor: QPoint,
        column_right: int,
        view_right_edge: int,
    ) -> QPoint:
        pop_w = self.width()
        pop_h = self.height()
        screen = self.screen()
        avail = screen.availableGeometry() if screen else None

        # Prefer right of column; flip to left when that would clip past the
        # view's right border.
        if column_right + 12 + pop_w <= view_right_edge - 12:
            x = column_right + 12
        else:
            x = anchor.x() - pop_w - 12

        y = anchor.y()

        if avail:
            if x + pop_w > avail.right():
                x = avail.right() - pop_w
            if x < avail.left():
                x = avail.left()
            if y + pop_h > avail.bottom():
                y = avail.bottom() - pop_h
            if y < avail.top():
                y = avail.top()

        return QPoint(x, y)

    def schedule_hide(self) -> None:
        """Start the re-entry-tolerance hide timer (called from the view)."""
        self._hide_timer.start()

    def enterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hide_timer.start()
        super().leaveEvent(event)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"ClusterPopover {{"
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
