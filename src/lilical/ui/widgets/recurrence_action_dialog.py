from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class RecurrenceActionDialog(QDialog):
    """Ask whether an edit/delete should apply to 'This occurrence' or 'Entire series'."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        action: str = "edit",
    ) -> None:
        super().__init__(parent)
        verb = "Edit" if action == "edit" else "Delete"
        self.setWindowTitle(f"{verb} recurring event")
        self._choice: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"This is a recurring event. {verb} which occurrences?")
        )

        btn_occurrence = QPushButton("This occurrence")
        btn_occurrence.setDefault(True)
        btn_occurrence.clicked.connect(lambda: self._pick("occurrence"))

        btn_series = QPushButton("Entire series")
        btn_series.clicked.connect(lambda: self._pick("series"))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        buttons.addButton(btn_occurrence, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(btn_series, QDialogButtonBox.ButtonRole.AcceptRole)

        layout.addWidget(buttons)

    def _pick(self, choice: str) -> None:
        self._choice = choice
        self.accept()

    @property
    def choice(self) -> str | None:
        """'occurrence', 'series', or None if cancelled."""
        return self._choice
