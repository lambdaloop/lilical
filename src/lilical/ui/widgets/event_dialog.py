from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lilical.models.event import Event


class EventDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        event: Event | None = None,
    ) -> None:
        super().__init__(parent)
        self._editing = event is not None
        self._original = event
        self.setWindowTitle("Edit event" if event else "New event")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._title_edit = QLineEdit()
        self._title_edit.setText(event.summary if event else "")
        form.addRow("Title:", self._title_edit)

        self._location_edit = QLineEdit()
        self._location_edit.setText(event.location if event else "")
        form.addRow("Location:", self._location_edit)

        self._calendar_combo = QComboBox()
        form.addRow("Calendar:", self._calendar_combo)

        self._notes_edit = QTextEdit()
        self._notes_edit.setText(event.description if event else "")
        form.addRow("Notes:", self._notes_edit)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def summary(self) -> str:
        return self._title_edit.text().strip()
