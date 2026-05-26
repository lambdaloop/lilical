from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from lilical.storage.event_store import EventStore


class CopyToCalendarDialog(QDialog):
    """Pick a writable destination calendar for a copy operation."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        store: "EventStore",
        source_calendar_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Copy to calendar")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Copy to calendar:"))

        self._cal_combo = QComboBox()
        for acc in store.list_accounts():
            for cal in store.list_calendars(acc.id, included_only=True):
                if (cal.access_role or "").lower() in ("reader", "freebusyreader"):
                    continue
                self._cal_combo.addItem(
                    f"{acc.display_name} / {cal.display_name}", cal.id
                )
        layout.addWidget(self._cal_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self._cal_combo.count() == 0:
            self._cal_combo.addItem("No writable calendars available")
            if self._ok_btn is not None:
                self._ok_btn.setEnabled(False)
        else:
            # Pre-select the source calendar if it is in the list.
            for i in range(self._cal_combo.count()):
                if self._cal_combo.itemData(i) == source_calendar_id:
                    self._cal_combo.setCurrentIndex(i)
                    break

    @property
    def calendar_id(self) -> str | None:
        return self._cal_combo.currentData()
