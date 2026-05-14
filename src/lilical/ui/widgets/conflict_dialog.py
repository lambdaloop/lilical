from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
)

from lilical.models.event import Event


class ConflictDialog(QDialog):
    def __init__(
        self,
        parent=None,
        local: Event | None = None,
        remote: Event | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync conflict")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"\"{local.summary if local else ''}\" was changed both here and on the server."
        ))

        self._local_radio = QRadioButton("Keep your version")
        self._remote_radio = QRadioButton("Use server version")
        self._merge_radio = QRadioButton("Merge")
        self._local_radio.setChecked(True)

        layout.addWidget(self._local_radio)
        layout.addWidget(self._remote_radio)
        layout.addWidget(self._merge_radio)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
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
