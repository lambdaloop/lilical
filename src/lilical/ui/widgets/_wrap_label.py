"""QLabel subclass whose minimumSizeHint width is 0.

QLabel.minimumSizeHint() for a word-wrapped label returns the width of the
longest unbreakable word.  A linkified URL can be hundreds of pixels wide,
which propagates up through the layout, holds the content widget wider than
the QScrollArea viewport, and clips every sibling label on the right edge.

This subclass keeps the heightForWidth chain intact while letting the parent
layout shrink the label down to the actual available width.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QLabel


class WrapLabel(QLabel):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.setWordWrap(True)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        base = super().minimumSizeHint()
        return QSize(0, base.height())
