from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from PySide6.QtCore import QDate, QDateTime, QTime
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from lilical.models.event import Event
    from lilical.storage.event_store import EventStore

# 16 distinct event colors (name → hex)
_EVENT_COLORS: list[tuple[str, str]] = [
    ("Tomato", "#e05050"),
    ("Flamingo", "#e07878"),
    ("Tangerine", "#e08030"),
    ("Banana", "#e0c830"),
    ("Sage", "#70a870"),
    ("Basil", "#3a7a3a"),
    ("Peacock", "#3a80c8"),
    ("Blueberry", "#3a50b8"),
    ("Lavender", "#9a78e0"),
    ("Grape", "#7a3aaa"),
    ("Graphite", "#8a8a8a"),
    ("Cyan", "#3ab8c8"),
    ("Default", ""),
]

_IANA_ZONES: list[str] = sorted(zoneinfo.available_timezones())


def _dt_to_qdatetime(dt: datetime | None) -> QDateTime:
    if dt is None:
        dt = datetime.now(timezone.utc)
    # Convert to local for display
    local = dt.astimezone()
    return QDateTime(
        QDate(local.year, local.month, local.day),
        QTime(local.hour, local.minute),
    )


def _qdatetime_to_dt(qdt: QDateTime, tz_name: str) -> datetime:
    d = qdt.date()
    t = qdt.time()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), 0, tzinfo=tz)


class _ColorButton(QToolButton):
    def __init__(
        self, name: str, hex_color: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._hex = hex_color
        self._selected = False
        self.setFixedSize(24, 24)
        self.setToolTip(name)
        self.setCheckable(True)
        self._update_style()

    def _update_style(self) -> None:
        if self._hex:
            border = "2px solid #ffffff" if self._selected else "1px solid #555"
            self.setStyleSheet(
                f"QToolButton {{ background-color: {self._hex}; "
                f"border: {border}; border-radius: 4px; }}"
            )
        else:
            border = "2px solid #ffffff" if self._selected else "1px solid #555"
            self.setStyleSheet(
                f"QToolButton {{ background-color: palette(base); "
                f"border: {border}; border-radius: 4px; }}"
            )

    def setSelected(self, v: bool) -> None:  # noqa: N802
        self._selected = v
        self.setChecked(v)
        self._update_style()

    @property
    def color_hex(self) -> str:
        return self._hex


class EventDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        store: "EventStore",
        event: "Event | None" = None,
        default_dt: datetime | None = None,
        default_dtend: datetime | None = None,
        default_all_day: bool = False,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._event = event
        self._invitees_edit = None  # set below once we know the contact store
        self._editing = event is not None
        self.setWindowTitle("Edit event" if event else "New event")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        # ── Title ──────────────────────────────────────────────────────────────
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Event title")
        if event:
            self._title_edit.setText(event.summary)
        form.addRow("Title:", self._title_edit)

        # ── Start / End ────────────────────────────────────────────────────────
        self._all_day_cb = QCheckBox("All day")

        # Prefill order: an explicit `event` (edit mode) wins; otherwise the
        # caller-supplied `default_dt` / `default_dtend` win over "now + 1 h".
        start_default = default_dt or datetime.now(timezone.utc)
        end_default = default_dtend or (start_default + timedelta(hours=1))
        if event:
            start_default = event.dtstart or start_default
            end_default = event.dtend or end_default

        self._start_edit = QDateTimeEdit()
        self._start_edit.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self._start_edit.setCalendarPopup(True)
        self._start_edit.setDateTime(_dt_to_qdatetime(start_default))

        self._end_edit = QDateTimeEdit()
        self._end_edit.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self._end_edit.setCalendarPopup(True)
        self._end_edit.setDateTime(_dt_to_qdatetime(end_default))

        dt_row = QHBoxLayout()
        dt_row.addWidget(QLabel("Start:"))
        dt_row.addWidget(self._start_edit)
        dt_row.addSpacing(12)
        dt_row.addWidget(QLabel("End:"))
        dt_row.addWidget(self._end_edit)
        dt_row.addSpacing(12)
        dt_row.addWidget(self._all_day_cb)
        dt_row.addStretch()
        form.addRow("", dt_row)

        if (event and event.all_day) or default_all_day:
            self._all_day_cb.setChecked(True)
            self._start_edit.setDisplayFormat("yyyy-MM-dd")
            self._end_edit.setDisplayFormat("yyyy-MM-dd")
            # dtend uses exclusive RFC 5545 convention (midnight of next day);
            # show the inclusive last day to the user
            ed_local = end_default.astimezone() if end_default.tzinfo else end_default
            if ed_local.hour == 0 and ed_local.minute == 0:
                self._end_edit.setDateTime(
                    _dt_to_qdatetime(end_default - timedelta(days=1))
                )

        self._all_day_cb.toggled.connect(self._on_all_day_toggled)

        # ── Timezone ───────────────────────────────────────────────────────────
        self._tz_combo = QComboBox()
        self._tz_combo.addItems(_IANA_ZONES)
        from lilical.utils.timezone import local_iana_tz

        local_iana = local_iana_tz()
        default_tz = (event.tz if event and event.tz else None) or local_iana
        if default_tz in _IANA_ZONES:
            self._tz_combo.setCurrentText(default_tz)
        else:
            self._tz_combo.setCurrentText(
                local_iana if local_iana in _IANA_ZONES else "UTC"
            )
        self._tz_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        form.addRow("Time zone:", self._tz_combo)

        # ── Calendar ───────────────────────────────────────────────────────────
        self._cal_combo = QComboBox()
        self._cal_ids: list[str] = []
        accs = store.list_accounts()
        for acc in accs:
            cals = store.list_calendars(acc.id, included_only=True)
            for cal in cals:
                self._cal_combo.addItem(
                    f"{acc.display_name} / {cal.display_name}", cal.id
                )
                self._cal_ids.append(cal.id)
        if event:
            for i in range(self._cal_combo.count()):
                if self._cal_combo.itemData(i) == event.calendar_id:
                    self._cal_combo.setCurrentIndex(i)
                    break
        form.addRow("Calendar:", self._cal_combo)

        # ── Color ──────────────────────────────────────────────────────────────
        color_row = QHBoxLayout()
        self._color_buttons: list[_ColorButton] = []
        current_color = (event.color if event else None) or ""
        for name, hex_val in _EVENT_COLORS:
            btn = _ColorButton(name, hex_val)
            btn.setSelected(
                hex_val == current_color or (not hex_val and not current_color)
            )
            btn.clicked.connect(lambda _checked=False, b=btn: self._select_color(b))
            color_row.addWidget(btn)
            self._color_buttons.append(btn)
        color_row.addStretch()
        form.addRow("Color:", color_row)

        # ── Location / URL ─────────────────────────────────────────────────────
        self._location_edit = QLineEdit()
        if event:
            self._location_edit.setText(event.location)
        form.addRow("Location:", self._location_edit)

        self._url_edit = QLineEdit()
        if event and event.url:
            self._url_edit.setText(event.url)
        form.addRow("URL:", self._url_edit)

        # ── Invitees ───────────────────────────────────────────────────────────
        from lilical.ui.widgets.contact_completer import InviteeChipEdit

        contact_store = getattr(store, "contacts", None)
        account_ids = [acc.id for acc in store.list_accounts(enabled_only=False)]
        self._invitees_edit = InviteeChipEdit(
            contact_store=contact_store,
            account_ids=account_ids,
            parent=self,
        )
        if event and event.attendees:
            self._invitees_edit.set_invitees(event.attendees)
        form.addRow("Invitees:", self._invitees_edit)

        # ── Notes ──────────────────────────────────────────────────────────────
        self._notes_edit = QTextEdit()
        self._notes_edit.setFixedHeight(80)
        if event:
            self._notes_edit.setPlainText(event.description)
        form.addRow("Notes:", self._notes_edit)

        # ── Recurrence ─────────────────────────────────────────────────────────
        from lilical.ui.widgets.recurrence_editor import RecurrenceEditor

        self._rrule_editor = RecurrenceEditor()
        if event and event.rrule:
            self._rrule_editor.set_value(event.rrule)
        form.addRow("Repeat:", self._rrule_editor)

        # ── Status / Visibility ────────────────────────────────────────────────
        status_box = QGroupBox()
        status_layout = QHBoxLayout(status_box)
        self._status_group = QButtonGroup(self)
        for label, val in [
            ("Confirmed", "CONFIRMED"),
            ("Tentative", "TENTATIVE"),
        ]:
            rb = QRadioButton(label)
            self._status_group.addButton(rb)
            rb.setProperty("status_val", val)
            if (event and event.status == val) or (not event and val == "CONFIRMED"):
                rb.setChecked(True)
            status_layout.addWidget(rb)
        status_layout.addStretch()
        form.addRow("Status:", status_box)

        vis_box = QGroupBox()
        vis_layout = QHBoxLayout(vis_box)
        self._transparency_group = QButtonGroup(self)
        for label, val in [("Busy", "OPAQUE"), ("Free", "TRANSPARENT")]:
            rb = QRadioButton(label)
            self._transparency_group.addButton(rb)
            rb.setProperty("trans_val", val)
            if (event and event.transparency == val) or (not event and val == "OPAQUE"):
                rb.setChecked(True)
            vis_layout.addWidget(rb)
        vis_layout.addStretch()
        form.addRow("Show as:", vis_box)

        layout.addLayout(form)

        # ── Buttons ────────────────────────────────────────────────────────────
        self.delete_requested = False
        btn_row = QHBoxLayout()
        if self._editing:
            from PySide6.QtWidgets import QPushButton

            delete_btn = QPushButton("Delete")
            delete_btn.setObjectName("deleteButton")
            delete_btn.clicked.connect(self._on_delete)
            btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

        # Focus the title field
        self._title_edit.setFocus()

    def _on_delete(self) -> None:
        self.delete_requested = True
        self.accept()

    def _on_all_day_toggled(self, checked: bool) -> None:
        fmt = "yyyy-MM-dd" if checked else "yyyy-MM-dd  HH:mm"
        self._start_edit.setDisplayFormat(fmt)
        self._end_edit.setDisplayFormat(fmt)

    def _select_color(self, clicked_btn: "_ColorButton") -> None:
        for btn in self._color_buttons:
            btn.setSelected(btn is clicked_btn)

    @property
    def calendar_id(self) -> str | None:
        return self._cal_combo.currentData()

    def _selected_color(self) -> str:
        for btn in self._color_buttons:
            if btn._selected:  # type: ignore[reportPrivateUsage]
                return btn.color_hex
        return ""

    def _selected_status(self) -> str:
        btn = self._status_group.checkedButton()
        return btn.property("status_val") if btn else "CONFIRMED"

    def _selected_transparency(self) -> str:
        btn = self._transparency_group.checkedButton()
        return btn.property("trans_val") if btn else "OPAQUE"

    def _on_save(self) -> None:
        if not self._title_edit.text().strip():
            QMessageBox.warning(
                self, "Missing title", "Please enter a title for the event."
            )
            self._title_edit.setFocus()
            return
        tz_name = self._tz_combo.currentText()
        start_dt = _qdatetime_to_dt(self._start_edit.dateTime(), tz_name)
        end_dt = _qdatetime_to_dt(self._end_edit.dateTime(), tz_name)
        if end_dt < start_dt:
            QMessageBox.warning(
                self, "Invalid time range", "End time must be on or after start time."
            )
            self._end_edit.setFocus()
            return
        self.accept()

    def build_event(self, uid: str) -> "Event":
        """Construct an Event dataclass from the current form state."""
        from lilical.models.event import Event

        tz_name = self._tz_combo.currentText()
        all_day = self._all_day_cb.isChecked()
        start_dt = _qdatetime_to_dt(self._start_edit.dateTime(), tz_name)
        end_dt = _qdatetime_to_dt(self._end_edit.dateTime(), tz_name)
        if all_day:
            # Dialog shows inclusive last day; convert to exclusive midnight of next day
            end_dt = datetime(
                end_dt.year, end_dt.month, end_dt.day, 0, 0, 0, tzinfo=end_dt.tzinfo
            ) + timedelta(days=1)
        cal_id = self.calendar_id or ""
        color = self._selected_color() or None
        url = self._url_edit.text().strip() or None
        rrule = self._rrule_editor.value()

        src = self._event
        invitees = tuple(self._invitees_edit.invitees()) if self._invitees_edit else ()
        # Re-attach the self attendee from the original event so our entry is preserved.
        if src and src.attendees:
            self_att = next((a for a in src.attendees if a.is_self), None)
            if self_att and not any(a.email == self_att.email for a in invitees):
                invitees = (self_att,) + invitees
        return Event(
            uid=uid,
            calendar_id=cal_id,
            provider_event_id=src.provider_event_id if src else None,
            recurrence_id=src.recurrence_id if src else None,
            rrule=rrule,
            exdates=src.exdates if src else (),
            rdates=src.rdates if src else (),
            sequence=src.sequence if src else 0,
            etag=src.etag if src else None,
            dtstart=start_dt,
            dtend=end_dt,
            tz=tz_name,
            all_day=all_day,
            summary=self._title_edit.text().strip(),
            description=self._notes_edit.toPlainText(),
            location=self._location_edit.text().strip(),
            url=url,
            color=color,
            status=self._selected_status(),
            transparency=self._selected_transparency(),
            attendees=invitees,
            organizer=src.organizer if src else None,
            local_dirty=True,
        )
