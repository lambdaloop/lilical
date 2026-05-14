from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


class AccountSetupDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add an account")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Step 1 of 3 — What kind of account?"))

        self._kind_combo = QComboBox()
        self._kind_combo.addItems(["Google Calendar", "Microsoft / Outlook", "CalDAV"])
        layout.addWidget(self._kind_combo)

        form = QFormLayout()
        self._name_edit = QLineEdit()
        form.addRow("Display name:", self._name_edit)
        self._server_edit = QLineEdit()
        self._server_edit.setPlaceholderText("https://caldav.example.com")
        form.addRow("Server URL:", self._server_edit)
        layout.addLayout(form)
        self._server_edit.setVisible(False)
        self._kind_combo.currentTextChanged.connect(self._on_kind_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_kind_changed(self, text: str) -> None:
        self._server_edit.setVisible(text == "CalDAV")
