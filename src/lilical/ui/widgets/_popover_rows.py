"""Shared data types and row-widget factory for event popovers."""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from lilical.ui import theme


class PopoverEvent(NamedTuple):
    time_str: str           # "All day" / "09:00" / "9:00 – 10:30 AM"
    title: str
    location: str | None
    calendar_color: str | None
    uid: str | None = None  # used by ClusterPopover for click-to-edit


def make_row(ev: PopoverEvent) -> QWidget:
    """Build one agenda row widget for a popover."""
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 1, 0, 1)
    hl.setSpacing(5)

    color = ev.calendar_color or theme.CHIP_FALLBACK
    swatch = QLabel()
    swatch.setFixedSize(8, 12)
    swatch.setStyleSheet(f"background: {color}; border-radius: 2px;")
    hl.addWidget(swatch, 0, Qt.AlignmentFlag.AlignVCenter)

    time_lbl = QLabel(ev.time_str)
    time_lbl.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY};"
        f" font-family: {theme.FONT_FAMILY};"
        f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
    )
    time_lbl.setFixedWidth(44)
    hl.addWidget(time_lbl, 0)

    display = ev.title or "(no title)"
    if ev.location:
        display = f"{display}  ·  {ev.location}"
    title_lbl = QLabel(display)
    title_lbl.setStyleSheet(
        f"color: {theme.TEXT_PRIMARY};"
        f" font-family: {theme.FONT_FAMILY};"
        f" font-size: {theme.FONT_CHIP_TITLE}pt;"
    )
    hl.addWidget(title_lbl, 1)

    return row
