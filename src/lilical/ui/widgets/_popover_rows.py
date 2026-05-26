"""Shared data types and row-widget factory for event popovers."""

from __future__ import annotations

from typing import Any, NamedTuple

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.ui.widgets._tight_title_label import TightTitleLabel


class PopoverEvent(NamedTuple):
    time_str: str           # "All day" / "09:00" / "9:00 – 10:30 AM"
    title: str
    location: str | None
    calendar_color: str | None
    uid: str | None = None        # propagated so views can look up the source event
    calendar_id: str | None = None  # used by inspector to resolve calendar name


def cluster_events_to_popover_events(
    events: list[dict[str, Any]], time_format: str
) -> list[PopoverEvent]:
    """Build a sorted PopoverEvent list from a cluster's raw event dicts."""
    from lilical.ui._time_fmt import fmt_hm

    result: list[PopoverEvent] = []
    for ev in sorted(events, key=lambda e: e["start_min"]):
        payload = ev["payload"]
        event = payload["event"]
        s = int(ev["start_min"])
        e = int(ev["end_min"])
        if time_format == "12h":
            s_str = fmt_hm(s // 60, s % 60, "12h")
            e_str = fmt_hm(e // 60, e % 60, "12h")
        else:
            s_str = f"{s // 60:02d}:{s % 60:02d}"
            e_str = f"{e // 60:02d}:{e % 60:02d}"
        result.append(
            PopoverEvent(
                time_str=f"{s_str} – {e_str}",
                title=event.summary or "(no title)",
                location=event.location or None,
                calendar_color=payload.get("cal_color"),
                uid=event.uid or None,
                calendar_id=event.calendar_id or None,
            )
        )
    return result


def make_row(ev: PopoverEvent) -> QWidget:
    """Build one agenda row widget for a popover."""
    row = QWidget()
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 1, 0, 1)
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
    vl.setSpacing(0)

    time_lbl = QLabel(ev.time_str)
    time_lbl.setStyleSheet(
        f"color: {theme.TEXT_SECONDARY};"
        f" font-family: {theme.FONT_FAMILY};"
        f" font-size: {theme.FONT_CHIP_PREFIX}pt;"
    )
    vl.addWidget(time_lbl)

    title_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_TITLE, QFont.Weight.Medium)
    title_lbl = TightTitleLabel(ev.title or "(no title)", title_font, max_lines=3)
    vl.addWidget(title_lbl)

    # Propagate heightForWidth so adjustSize() on the popover grows for wraps.
    sp = text_col.sizePolicy()
    sp.setHeightForWidth(True)
    text_col.setSizePolicy(sp)

    hl.addWidget(text_col, 1)
    return row
