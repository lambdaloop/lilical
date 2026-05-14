from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lilical.models.event import Event


def _fmt(v: object) -> str:
    if v is None or v == "" or v == ():
        return "—"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or "—"
    return str(v)


_COMPARED_FIELDS: list[tuple[str, str]] = [
    ("summary", "Title"),
    ("dtstart", "Start"),
    ("dtend", "End"),
    ("location", "Location"),
    ("rrule", "Repeats"),
    ("status", "Status"),
    ("transparency", "Show as"),
]


class ConflictDialog(QDialog):
    def __init__(
        self,
        parent=None,
        local: Event | None = None,
        remote: Event | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync conflict")
        self.setMinimumWidth(560)

        # Resolve display name from whichever side has data.
        name = (local.summary if local else None) or (remote.summary if remote else None) or "(unnamed)"

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f'<b>"{name}"</b> was changed both here and on the server.')
        )

        # ── Field comparison table ──────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        grid_container = QWidget()
        grid = QGridLayout(grid_container)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)

        header_local = QLabel("<b>Your version</b>")
        header_remote = QLabel("<b>Server version</b>")
        header_local.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_remote.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel(""), 0, 0)
        grid.addWidget(header_local, 0, 1)
        grid.addWidget(header_remote, 0, 2)

        for row, (attr, label) in enumerate(_COMPARED_FIELDS, start=1):
            lv = _fmt(getattr(local, attr, None) if local else None)
            rv = _fmt(getattr(remote, attr, None) if remote else None)
            differs = lv != rv

            field_label = QLabel(f"{label}:")
            field_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lv_label = QLabel(lv)
            rv_label = QLabel(rv)
            if differs:
                warn = " ⚠"
                lv_label.setText(lv + warn)
                rv_label.setText(rv + warn)
                lv_label.setStyleSheet("color: #ff6b6b;")
                rv_label.setStyleSheet("color: #ff6b6b;")

            grid.addWidget(field_label, row, 0)
            grid.addWidget(lv_label, row, 1)
            grid.addWidget(rv_label, row, 2)

        scroll.setWidget(grid_container)
        layout.addWidget(scroll)

        # ── Resolution options ──────────────────────────────────────────────
        self._local_radio = QRadioButton("Keep your version (overwrite server)")
        self._remote_radio = QRadioButton("Use server version (discard your changes)")
        self._merge_radio = QRadioButton("Merge: I'll edit before saving")
        self._local_radio.setChecked(True)

        layout.addWidget(self._local_radio)
        layout.addWidget(self._remote_radio)
        layout.addWidget(self._merge_radio)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def choice(self) -> str:
        if self._local_radio.isChecked():
            return "local"
        if self._remote_radio.isChecked():
            return "remote"
        return "merge"
