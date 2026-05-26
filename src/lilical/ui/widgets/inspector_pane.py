"""Persistent right-side inspector pane for hovered events and clusters."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.ui._notes_fmt import format_notes_html
from lilical.ui.widgets._popover_rows import PopoverEvent, make_row
from lilical.ui.widgets._wrap_label import WrapLabel


class InspectorPane(QWidget):
    """Right-side pane that mirrors the hovered event / cluster.

    Two stacked sections inside a QScrollArea:
      - Top: details of the single hovered event (title, time, location,
        calendar color + name, notes).
      - Bottom (cluster context): only shown when hovering an event that
        is part of a dense overlap cluster; lists the sibling events using
        the same `make_row` factory that the legacy popover used.

    Empty when nothing is hovered.
    """

    def __init__(
        self,
        cal_info_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._cal_info_provider = cal_info_provider
        self.setMinimumWidth(180)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(12, 12, 12, 12)
        self._content_layout.setSpacing(6)

        # ── Top section: hovered event details ─────────────────────────
        self._hovered_label = QLabel("HOVERED")
        self._content_layout.addWidget(self._hovered_label)

        title_font = QFont()
        title_font.setPointSize(max(11, theme.FONT_BASE + 2))
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title = WrapLabel()
        self._title.setFont(title_font)
        # Cap at 3 lines (font metrics * 3 lines + small slack).
        self._title.setMaximumHeight(QFontMetrics(title_font).lineSpacing() * 3 + 2)
        self._content_layout.addWidget(self._title)

        self._time = QLabel()
        self._content_layout.addWidget(self._time)

        self._location = WrapLabel()
        self._content_layout.addWidget(self._location)

        self._calendar_row = QWidget()
        cal_layout = QHBoxLayout(self._calendar_row)
        cal_layout.setContentsMargins(0, 0, 0, 0)
        cal_layout.setSpacing(6)
        self._calendar_swatch = QFrame()
        self._calendar_swatch.setFixedSize(10, 10)
        self._calendar_name = WrapLabel()
        cal_layout.addWidget(self._calendar_swatch, 0, Qt.AlignmentFlag.AlignVCenter)
        cal_layout.addWidget(self._calendar_name, 1)
        self._content_layout.addWidget(self._calendar_row)

        self._notes_header = QLabel("NOTES")
        self._content_layout.addWidget(self._notes_header)
        self._notes = WrapLabel()
        self._notes.setTextFormat(Qt.TextFormat.RichText)
        self._notes.setOpenExternalLinks(True)
        self._notes.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._content_layout.addWidget(self._notes)

        # ── Separator between sections ─────────────────────────────────
        self._separator = QFrame()
        self._separator.setFrameShape(QFrame.Shape.HLine)
        self._separator.setFixedHeight(1)
        self._content_layout.addWidget(self._separator)

        # ── Bottom section: cluster siblings ───────────────────────────
        self._cluster_header = QLabel()
        self._content_layout.addWidget(self._cluster_header)

        self._cluster_rows_widget = QWidget()
        self._cluster_rows_layout = QVBoxLayout(self._cluster_rows_widget)
        self._cluster_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._cluster_rows_layout.setSpacing(2)
        self._content_layout.addWidget(self._cluster_rows_widget)

        self._content_layout.addStretch(1)

        self._current_rows: list[QWidget] = []
        self._apply_theme()
        self.clear()

    # ── Public API ─────────────────────────────────────────────────────

    def show_event(
        self, ev: PopoverEvent, notes: str | None = None
    ) -> None:
        """Populate the top section with one event's details; clear cluster section."""
        self._apply_theme()
        self._populate_event_section(ev, notes)
        self._set_cluster_section(None, [])

    def show_cluster(
        self,
        primary: PopoverEvent,
        siblings: list[PopoverEvent],
    ) -> None:
        """Populate both sections: top = `primary` (no notes), bottom = `siblings`."""
        self._apply_theme()
        self._populate_event_section(primary, notes=None)
        self._set_cluster_section(primary, siblings)

    def clear(self) -> None:
        """Empty both sections — pane goes blank."""
        self._hovered_label.setVisible(False)
        self._title.clear()
        self._title.setVisible(False)
        self._time.clear()
        self._time.setVisible(False)
        self._location.clear()
        self._location.setVisible(False)
        self._calendar_row.setVisible(False)
        self._notes_header.setVisible(False)
        self._notes.clear()
        self._notes.setVisible(False)
        self._set_cluster_section(None, [])

    # ── Internal helpers ───────────────────────────────────────────────

    def _resolve_calendar_name(self, calendar_id: str | None) -> str | None:
        if not calendar_id or self._cal_info_provider is None:
            return None
        info = self._cal_info_provider().get(calendar_id)
        return getattr(info, "display_name", None) or None

    def _populate_event_section(
        self, ev: PopoverEvent, notes: str | None
    ) -> None:
        self._hovered_label.setVisible(True)
        self._title.setText(ev.title or "(no title)")
        self._title.setVisible(True)
        self._time.setText(ev.time_str)
        self._time.setVisible(True)

        if ev.location:
            self._location.setText(f"📍 {ev.location}")
            self._location.setVisible(True)
        else:
            self._location.clear()
            self._location.setVisible(False)

        color = ev.calendar_color or theme.CHIP_FALLBACK
        self._calendar_swatch.setStyleSheet(
            f"background: {color}; border-radius: 5px;"
        )
        cal_name = self._resolve_calendar_name(ev.calendar_id)
        if cal_name:
            self._calendar_name.setText(cal_name)
        else:
            self._calendar_name.clear()
        self._calendar_row.setVisible(True)

        notes_text = (notes or "").strip()
        if notes_text:
            self._notes_header.setVisible(True)
            self._notes.setText(format_notes_html(notes_text))
            self._notes.setVisible(True)
        else:
            self._notes_header.setVisible(False)
            self._notes.clear()
            self._notes.setVisible(False)

    def _set_cluster_section(
        self,
        primary: PopoverEvent | None,
        siblings: list[PopoverEvent],
    ) -> None:
        for row in self._current_rows:
            self._cluster_rows_layout.removeWidget(row)
            row.deleteLater()
        self._current_rows.clear()

        if not siblings:
            self._separator.setVisible(False)
            self._cluster_header.setVisible(False)
            self._cluster_rows_widget.setVisible(False)
            return

        self._separator.setVisible(True)
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

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"InspectorPane {{ background: {theme.BG_SURFACE}; }}"
            f" InspectorPane QWidget {{ background: transparent; }}"
            f" QScrollArea {{ background: {theme.BG_SURFACE}; border: none; }}"
        )
        meta_style = (
            f"color: {theme.TEXT_DISABLED};"
            f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
            f" letter-spacing: 1px;"
            f" font-weight: bold;"
        )
        self._hovered_label.setStyleSheet(meta_style)
        self._cluster_header.setStyleSheet(meta_style)
        self._notes_header.setStyleSheet(meta_style)
        self._title.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
        self._time.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FONT_CHIP_TITLE}pt;"
        )
        self._location.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FONT_CHIP_LOCATION}pt;"
        )
        self._notes.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FONT_CHIP_LOCATION}pt;"
        )
        self._calendar_name.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-size: {theme.FONT_CHIP_LOCATION}pt;"
        )
        self._separator.setStyleSheet(f"background: {theme.BORDER};")
