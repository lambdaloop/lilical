"""Translucent ghost rectangle reused by every drag interaction.

The same item is used for drag-to-create, drag-to-move, and drag-to-resize on
both Week and Day views. The owning view manages the rect + label and removes
the item from the scene when the drag commits or cancels.
"""

from __future__ import annotations

from typing import override

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem

from lilical.ui import theme

_Z_VALUE = 200  # above chips (default Z=0) and the sticky header (Z=100)
_FILL_ALPHA = 90  # ~30% — enough to read the chip behind without overpowering


class DragPreview(QGraphicsItem):
    """Semi-transparent rect + centered single-line label."""

    def __init__(self, rect: QRectF, label: str = "") -> None:
        super().__init__()
        self._rect = QRectF(rect)
        self._label = label
        self.setZValue(_Z_VALUE)
        # Don't intercept mouse events — the view's drag controller already
        # has the press; the preview is purely visual.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def set_rect(self, rect: QRectF) -> None:
        if rect == self._rect:
            return
        self.prepareGeometryChange()
        self._rect = QRectF(rect)

    def set_label(self, label: str) -> None:
        if label == self._label:
            return
        self._label = label
        self.update()

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(theme.ACCENT_FILL)
        fill.setAlpha(_FILL_ALPHA)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(theme.ACCENT), 1))
        painter.drawRoundedRect(self._rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)

        if not self._label:
            return
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX, QFont.Weight.Bold)
        )
        painter.drawText(
            self._rect.adjusted(4, 2, -4, -2),
            int(Qt.AlignmentFlag.AlignCenter),
            self._label,
        )
