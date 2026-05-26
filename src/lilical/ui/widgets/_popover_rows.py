"""Shared data types and row-widget factory for event popovers."""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
    hl.setContentsMargins(0, 2, 0, 2)
    hl.setSpacing(6)

    color = ev.calendar_color or theme.CHIP_FALLBACK
    swatch = QFrame()
    swatch.setFixedWidth(3)
    swatch.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
    swatch.setStyleSheet(f"background: {color}; border-radius: 1px;")
    hl.addWidget(swatch)

    text_col = QWidget()
    vl = QVBoxLayout(text_col)
    vl.setContentsMargins(0, 0, 0, 0)
    vl.setSpacing(1)

    time_lbl = QLabel(ev.time_str)
    time_lbl.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY};"
        f" font-family: {theme.FONT_FAMILY};"
        f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
    )
    vl.addWidget(time_lbl)

    title_lbl = QLabel(ev.title or "(no title)")
    title_lbl.setWordWrap(True)
    title_lbl.setStyleSheet(
        f"color: {theme.TEXT_PRIMARY};"
        f" font-family: {theme.FONT_FAMILY};"
        f" font-size: {theme.FONT_CHIP_TITLE}pt;"
        f" font-weight: 600;"
    )
    vl.addWidget(title_lbl)

    if ev.location:
        loc_lbl = QLabel(ev.location)
        loc_lbl.setWordWrap(True)
        loc_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY};"
            f" font-family: {theme.FONT_FAMILY};"
            f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
        )
        vl.addWidget(loc_lbl)

    hl.addWidget(text_col, 1)
    return row
