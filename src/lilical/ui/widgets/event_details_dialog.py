from __future__ import annotations

import re
from datetime import timedelta
from html import escape
from typing import TYPE_CHECKING

_URL_RE = re.compile(r"(https?://[^\s<>\"{}|\\^`\[\]]+)", re.IGNORECASE)
# Conservative HTML detection: look for common block/inline tags used by calendar systems.
_HTML_TAG_RE = re.compile(
    r"<(?:html|body|p|br|div|span|a|b|i|em|strong|ul|ol|li|h[1-6]|table|tr|td|pre|img)"
    r"[\s/>]",
    re.IGNORECASE,
)

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lilical.ui.widgets.event_chip import _readable_text_color, _resolve_color

if TYPE_CHECKING:
    from lilical.models.event import Event
    from lilical.storage.event_store import EventStore

_DFMT = "%a, %b %-d, %Y"
_DFMT_NO_YEAR = "%a, %b %-d"


class EventDetailsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        store: "EventStore",
        event: "Event",
    ) -> None:
        super().__init__(parent)
        summary = event.summary or ""
        title = f"Event details — {summary[:50]}{'…' if len(summary) > 50 else ''}" if summary else "Event details"
        self.setWindowTitle(title)
        self.setMinimumWidth(500)

        self.edit_requested: bool = False
        self.delete_requested: bool = False

        time_fmt = str(QSettings().value("time_format", "24h") or "24h")

        # Resolve event color for the header strip.
        cal = store.get_calendar(event.calendar_id)
        cal_color = cal.color if cal else None
        bg = _resolve_color(event.color, cal_color)
        fg = _readable_text_color(bg)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Colored header strip ──────────────────────────────────────────────
        header = QWidget()
        header.setAutoFillBackground(True)
        pal = header.palette()
        pal.setColor(QPalette.ColorRole.Window, bg)
        header.setPalette(pal)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)

        title_label = QLabel(summary or "(No title)")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        if event.status == "CANCELLED":
            title_font.setStrikeOut(True)
        title_label.setFont(title_font)
        title_label.setWordWrap(True)
        fg_pal = title_label.palette()
        fg_pal.setColor(QPalette.ColorRole.WindowText, fg)
        title_label.setPalette(fg_pal)
        title_label.setForegroundRole(QPalette.ColorRole.WindowText)
        header_layout.addWidget(title_label)
        outer.addWidget(header)

        # ── Scrollable body ───────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        body_widget = QWidget()
        body_layout = QVBoxLayout(body_widget)
        body_layout.setContentsMargins(16, 16, 16, 8)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(16)

        def _add_row(label: str, text: str) -> None:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            form.addRow(label, lbl)

        # ── When ─────────────────────────────────────────────────────────────
        when = _format_when(event, time_fmt)
        if when:
            _add_row("When:", when)

        # ── Calendar ─────────────────────────────────────────────────────────
        cal_label = _format_calendar(store, event.calendar_id)
        if cal_label:
            _add_row("Calendar:", cal_label)

        # ── Location ─────────────────────────────────────────────────────────
        if event.location:
            _add_row("Location:", event.location)

        # ── Notes ─────────────────────────────────────────────────────────────
        if event.description:
            notes_text = (
                event.description
                if _is_html(event.description)
                else _linkify(event.description)
            )
            notes_lbl = QLabel(notes_text)
            notes_lbl.setTextFormat(Qt.TextFormat.RichText)
            notes_lbl.setOpenExternalLinks(True)
            notes_lbl.setWordWrap(True)
            notes_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            notes_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            form.addRow("Notes:", notes_lbl)

        # ── Recurrence ────────────────────────────────────────────────────────
        rec = _format_recurrence(event)
        if rec:
            _add_row("Recurrence:", rec)

        # ── Status ────────────────────────────────────────────────────────────
        if event.status and event.status != "CONFIRMED":
            _add_row("Status:", event.status.title())

        # ── Show as ──────────────────────────────────────────────────────────
        if event.transparency == "TRANSPARENT":
            _add_row("Show as:", "Free")

        # ── My response ───────────────────────────────────────────────────────
        if event.self_response:
            _add_row("My response:", event.self_response.title())

        # ── Attendees ─────────────────────────────────────────────────────────
        if event.attendees:
            _add_row("Attendees:", "\n".join(_format_attendee(a) for a in event.attendees))

        # ── Categories ────────────────────────────────────────────────────────
        if event.categories:
            _add_row("Categories:", ", ".join(event.categories))

        # ── Alarms ────────────────────────────────────────────────────────────
        if event.valarms:
            _add_row("Alarms:", "\n".join(event.valarms))

        body_layout.addLayout(form)
        body_layout.addStretch()
        scroll.setWidget(body_widget)
        outer.addWidget(scroll)

        # ── Button row ────────────────────────────────────────────────────────
        btn_bar = QWidget()
        btn_bar_layout = QHBoxLayout(btn_bar)
        btn_bar_layout.setContentsMargins(12, 8, 12, 12)
        btn_bar_layout.addStretch()

        edit_btn = QPushButton("Edit")
        delete_btn = QPushButton("Delete")
        close_btn = QPushButton("Close")

        edit_btn.setDefault(False)
        close_btn.setDefault(True)

        edit_btn.clicked.connect(self._on_edit)
        delete_btn.clicked.connect(self._on_delete)
        close_btn.clicked.connect(self.reject)

        btn_bar_layout.addWidget(edit_btn)
        btn_bar_layout.addWidget(delete_btn)
        btn_bar_layout.addWidget(close_btn)
        outer.addWidget(btn_bar)

    def _on_edit(self) -> None:
        self.edit_requested = True
        self.accept()

    def _on_delete(self) -> None:
        self.delete_requested = True
        self.accept()


def _format_when(event: "Event", time_fmt: str = "24h") -> str:
    if event.dtstart is None:
        return ""
    dtstart = event.dtstart
    dtend = event.dtend
    tfmt = "%-I:%M %p" if time_fmt == "12h" else "%H:%M"

    # Build timezone suffix: only show when the event timezone differs from local.
    tz_suffix = ""
    if event.tz and event.tz not in ("UTC",):
        try:
            from lilical.utils.timezone import local_iana_tz
            if event.tz != local_iana_tz():
                tz_suffix = f"  ({event.tz})"
        except Exception:
            pass

    if event.all_day:
        start_d = dtstart.date() if hasattr(dtstart, "date") else dtstart
        if dtend is not None:
            end_d = dtend.date() if hasattr(dtend, "date") else dtend
            # RFC 5545 all-day dtend is exclusive midnight; show inclusive last day.
            inclusive_end = end_d - timedelta(days=1)
            if inclusive_end == start_d:
                return f"{start_d.strftime(_DFMT)}  ·  All day"
            # Multi-day: omit year on start when same year as end.
            if start_d.year == inclusive_end.year:
                return (
                    f"{start_d.strftime(_DFMT_NO_YEAR)} → "
                    f"{inclusive_end.strftime(_DFMT)}  ·  All day"
                )
            return (
                f"{start_d.strftime(_DFMT)} → "
                f"{inclusive_end.strftime(_DFMT)}  ·  All day"
            )
        return f"{start_d.strftime(_DFMT)}  ·  All day"

    # Timed event — compare local dates for same-day detection.
    start_local = dtstart.astimezone()
    start_date = start_local.date()

    if dtend is not None:
        end_local = dtend.astimezone()
        end_date = end_local.date()
        if start_date == end_date:
            # Same day: "Mon, May 18, 2026  ·  14:00 – 15:00"
            return (
                f"{start_date.strftime(_DFMT)}  ·  "
                f"{start_local.strftime(tfmt)} – {end_local.strftime(tfmt)}"
                f"{tz_suffix}"
            )
        # Multi-day: "Mon, May 18, 14:00 → Tue, May 19, 09:00 (2026)"
        year_suffix = f"  ({start_local.year})" if start_local.year == end_local.year else ""
        return (
            f"{start_date.strftime(_DFMT_NO_YEAR)}, {start_local.strftime(tfmt)}"
            f" → "
            f"{end_date.strftime(_DFMT_NO_YEAR)}, {end_local.strftime(tfmt)}"
            f"{year_suffix}{tz_suffix}"
        )

    return f"{start_date.strftime(_DFMT)}  ·  {start_local.strftime(tfmt)}{tz_suffix}"


def _format_calendar(store: "EventStore", calendar_id: str) -> str:
    for acc in store.list_accounts():
        for cal in store.list_calendars(acc.id, visible_only=False):
            if cal.id == calendar_id:
                return f"{acc.display_name} / {cal.display_name}"
    return calendar_id


def _format_recurrence(event: "Event") -> str:
    from lilical.ui.widgets.recurrence_editor import format_rrule_human

    parts: list[str] = []
    if event.rrule:
        parts.append(format_rrule_human(event.rrule))
    if event.recurrence_id is not None and not event.rrule:
        # This is a modified occurrence of a master series; show a human note
        # rather than "Override of occurrence on …".
        parts.append("Modified occurrence of a recurring series")
    return "\n".join(parts)


def _is_html(text: str) -> bool:
    """Return True if text appears to contain HTML markup."""
    stripped = text.lstrip()
    return stripped.startswith("<!DOCTYPE") or bool(_HTML_TAG_RE.search(text))


def _linkify(text: str) -> str:
    """Escape plain text for HTML, wrapping http(s) URLs in clickable links."""
    parts = _URL_RE.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # capturing group matches land at odd indices
            out.append(f'<a href="{escape(part)}">{escape(part)}</a>')
        else:
            out.append(escape(part).replace("\n", "<br>"))
    return "".join(out)


def _format_attendee(raw: str) -> str:
    # Strip iCal parameters (e.g. "RSVP=TRUE:mailto:foo@bar.com" → "foo@bar.com").
    if ":" in raw:
        raw = raw.rsplit(":", 1)[-1]
    # Strip mailto: prefix (case-insensitive).
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    return raw
