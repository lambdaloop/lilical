from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore


class _CalendarRow(QWidget):
    def __init__(
        self, calendar_id: str, display_name: str, color: str, checked: bool
    ) -> None:
        super().__init__()
        self._calendar_id = calendar_id
        self._checked = checked

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        self._checkbox = QCheckBox()
        self._checkbox.setChecked(checked)
        layout.addWidget(self._checkbox)

        swatch = QLabel()
        swatch.setFixedSize(14, 14)
        swatch.setStyleSheet(
            f"background-color: {color}; border-radius: 3px;"
        )
        layout.addWidget(swatch)

        label = QLabel(display_name)
        layout.addWidget(label, 1)

    @property
    def calendar_id(self) -> str:
        return self._calendar_id

    @property
    def is_checked(self) -> bool:
        return self._checkbox.isChecked()


class CalendarPickerDialog(QDialog):
    def __init__(
        self, parent: QWidget | None, account_id: str, store: EventStore
    ) -> None:
        super().__init__(parent)
        self._account_id = account_id
        self._store = store
        self._rows: list[_CalendarRow] = []

        acc = self._store.get_account(account_id)
        name = acc.display_name if acc else account_id
        self.setWindowTitle(f"Choose calendars — {name}")
        self.setMinimumWidth(380)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self._list_layout = QVBoxLayout(scroll_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch(1)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        for cal in self._store.list_calendars(self._account_id, visible_only=False):
            row = _CalendarRow(
                cal.id, cal.display_name, cal.color or "#5e9fff", bool(cal.is_visible)
            )
            self._rows.append(row)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        for row in self._rows:
            self._store.set_calendar_visibility(row.calendar_id, row.is_checked)
        self.accept()
