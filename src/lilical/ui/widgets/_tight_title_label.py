"""QWidget that renders a title with the same tight line-height as EventChip."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import QWidget

from lilical.ui import theme
from lilical.ui._text_layout import draw_tight_wrapped, tight_line_step


class TightTitleLabel(QWidget):
    """Title text rendered with the chip's tight line step.

    Uses draw_tight_wrapped so the per-line advance matches EventChip exactly
    (ascent+descent * 0.85, no leading).  Reports heightForWidth so the parent
    QVBoxLayout sizes the row to the actual wrapped content.
    """

    def __init__(
        self,
        text: str = "",
        font: QFont | None = None,
        max_lines: int = 3,
    ) -> None:
        super().__init__()
        self._text = text
        self._font = font or QFont(theme.FONT_FAMILY, theme.FONT_CHIP_TITLE)
        self._max_lines = max_lines
        self._line_step = tight_line_step(self._font)
        self._color = QColor(theme.TEXT_PRIMARY)

    def setText(self, text: str) -> None:  # noqa: N802
        self._text = text
        self.updateGeometry()
        self.update()

    def setColor(self, color: QColor) -> None:  # noqa: N802
        self._color = color
        self.update()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if not self._text or width <= 0:
            return self._line_step
        layout = QTextLayout(self._text, self._font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(opt)
        layout.beginLayout()
        count = 0
        while count < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(float(width))
            count += 1
        layout.endLayout()
        return max(count, 1) * self._line_step

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._line_step)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._line_step * self._max_lines)

    def paintEvent(self, _event: object) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(self._color)
        rect = QRectF(0, 0, self.width(), self.height())
        draw_tight_wrapped(painter, self._text, self._font, rect)
