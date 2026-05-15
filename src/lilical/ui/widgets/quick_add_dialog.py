from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore
from lilical.utils.timezone import local_iana_tz

log = logging.getLogger(__name__)


def _parse_natural(text: str) -> dict | None:
    """Parse a natural-language event string via dateparser.
    Returns dict with keys: title, dtstart, dtend, location or None on failure.
    """
    try:
        import dateparser
    except ImportError:
        return None

    # Simple extraction: split off a time phrase and use the remainder as title.
    # We try parsing the whole string as a date first, then fall back.
    settings = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": True}
    parsed_dt = dateparser.parse(text, settings=settings)
    if parsed_dt:
        # Heuristic: title is the text up to the first time keyword
        title = text.strip()
        return {
            "title": title,
            "dtstart": parsed_dt,
            "dtend": parsed_dt + timedelta(hours=1),
            "location": "",
        }
    return None


class QuickAddDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        store: EventStore,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._parsed: dict | None = None

        self.setWindowTitle("Quick add")
        self.setMinimumWidth(420)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Popup)

        layout = QVBoxLayout(self)

        self._input = QLineEdit()
        self._input.setPlaceholderText('E.g. "Lunch with Anna tomorrow at 1pm"')
        layout.addWidget(self._input)

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet("color: #c8c8c8; font-style: italic;")
        self._preview.setText("Start typing to preview...")
        layout.addWidget(self._preview)

        # Calendar picker
        self._cal_combo = QComboBox()
        accs = store.list_accounts()
        for acc in accs:
            cals = store.list_calendars(acc.id, visible_only=False)
            for cal in cals:
                self._cal_combo.addItem(
                    f"{acc.display_name} / {cal.display_name}", cal.id
                )
        layout.addWidget(self._cal_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._input.textChanged.connect(self._on_text_changed)
        self._input.setFocus()

    def _on_text_changed(self, text: str) -> None:
        if not text.strip():
            self._preview.setText("Start typing to preview...")
            self._parsed = None
            return
        result = _parse_natural(text)
        self._parsed = result
        if result and result.get("dtstart"):
            start: datetime = result["dtstart"]
            end: datetime = result["dtend"]
            local_start = start.astimezone()
            local_end = end.astimezone()
            title = result.get("title", text)
            self._preview.setText(
                f"{local_start.strftime('%a %b %-d, %H:%M')} – {local_end.strftime('%H:%M')}  ·  {title}"
            )
        else:
            self._preview.setText(
                "Could not parse a date/time — will save with current time."
            )

    def _on_save(self) -> None:
        import uuid
        from lilical.models.event import Event
        from PySide6.QtWidgets import QMessageBox

        text = self._input.text().strip()
        if not text:
            return

        cal_id = self._cal_combo.currentData() or ""
        if not cal_id:
            QMessageBox.warning(self, "No calendar", "Please add an account first.")
            return
        now = datetime.now(timezone.utc)

        if self._parsed and self._parsed.get("dtstart"):
            dtstart = self._parsed["dtstart"]
            dtend = self._parsed["dtend"]
            summary = text
        else:
            dtstart = now
            dtend = now + timedelta(hours=1)
            summary = text

        event = Event(
            uid=str(uuid.uuid4()),
            calendar_id=cal_id,
            dtstart=dtstart,
            dtend=dtend,
            tz=local_iana_tz(),
            summary=summary,
            local_dirty=True,
        )
        try:
            self._store.queue_create(event)
        except Exception:
            log.exception("QuickAdd: failed to create event")
        self.accept()
