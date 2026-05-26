"""Shared text-layout helpers used by EventChip and inspector widgets."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QTextLayout, QTextOption


def tight_line_step(font: QFont) -> int:
    """Return the per-line advance used by the chip's tight-wrap renderer."""
    fm = QFontMetricsF(font)
    return round((fm.ascent() + fm.descent()) * 0.85)


def draw_tight_wrapped(
    painter: QPainter, text: str, font: QFont, rect: QRectF
) -> None:
    """Word-wrap *text* into *rect* with no font leading between lines.

    Uses ascent+descent as the line step instead of height() (which adds
    leading, typically 1-2 px at 9 pt). Caller must set a clip rect; lines
    that extend past rect.height() are silently discarded.
    """
    fm = QFontMetricsF(font)
    line_step = round((fm.ascent() + fm.descent()) * 0.85)
    max_h = rect.height()

    layout = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    layout.setTextOption(opt)

    layout.beginLayout()
    y = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        y += line_step
        if y >= max_h:
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(rect.width())
                line.setPosition(QPointF(0, max_h + line_step))
            break
    layout.endLayout()
    layout.draw(painter, rect.topLeft())
