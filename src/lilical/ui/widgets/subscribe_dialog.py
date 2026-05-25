from __future__ import annotations

import asyncio
import hashlib
import logging

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.backends.base import PermanentError, TransientError
from lilical.ics.fetch import canonicalize_source, fetch_ics
from lilical.ics.importer import parse_ics_to_events
from lilical.models.event import Event

log = logging.getLogger(__name__)


class _FetchWorker(QObject):
    """Run fetch + parse off the GUI thread; emit result via Qt signal."""

    finished = Signal(object)  # tuple[bool, str, list[Event], str | None, str]
    # (success, message, events, suggested_name, content_sha256)

    def __init__(self, source: str) -> None:
        super().__init__()
        self._source = source

    def run(self) -> None:
        try:
            canon = canonicalize_source(self._source)
        except ValueError as e:
            self.finished.emit((False, str(e), [], None, ""))
            return
        try:
            loop = asyncio.new_event_loop()
            try:
                body, _etag, _last_mod = loop.run_until_complete(
                    fetch_ics(canon)
                )
            finally:
                loop.close()
        except PermanentError as e:
            self.finished.emit((False, str(e), [], None, ""))
            return
        except TransientError as e:
            self.finished.emit((False, f"could not fetch: {e}", [], None, ""))
            return
        except Exception as e:  # noqa: BLE001
            log.exception("subscribe fetch failed")
            self.finished.emit((False, f"unexpected error: {e}", [], None, ""))
            return
        if body is None:
            self.finished.emit((False, "source returned no data", [], None, ""))
            return
        sha = hashlib.sha256(body).hexdigest()
        try:
            events, suggested = parse_ics_to_events(body, calendar_id="")
        except Exception as e:  # noqa: BLE001
            log.exception("subscribe parse failed")
            self.finished.emit((False, f"not a valid ICS file: {e}", [], None, ""))
            return
        self.finished.emit((True, "", events, suggested, sha))


class SubscribeDialog(QDialog):
    """Modal dialog for subscribing to an ICS source.

    On accept, exposes:
      - canonical_source: str (https://, file:///…)
      - display_name: str (user-entered or X-WR-CALNAME, never empty)
      - color: str ("#RRGGBB")
      - events: list[Event] (already parsed; persist as-is)
      - content_sha256: str (seed for the cursor)
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Subscribe to calendar")
        self.setMinimumWidth(480)

        self.canonical_source: str = ""
        self.display_name: str = ""
        self.color: str = "#5e9fff"
        self.events: list[Event] = []
        self.content_sha256: str = ""

        self._worker_thread: QThread | None = None
        self._worker: _FetchWorker | None = None

        outer = QVBoxLayout(self)

        # ── Source type radio ─────────────────────────────────────────────
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Source:"))
        self._radio_web = QRadioButton("Web URL")
        self._radio_file = QRadioButton("Local file")
        self._radio_web.setChecked(True)
        self._type_group = QButtonGroup(self)
        self._type_group.addButton(self._radio_web)
        self._type_group.addButton(self._radio_file)
        type_row.addWidget(self._radio_web)
        type_row.addWidget(self._radio_file)
        type_row.addStretch(1)
        outer.addLayout(type_row)

        # ── Form: URL/Path, name, color ───────────────────────────────────
        form = QFormLayout()

        path_row = QHBoxLayout()
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText("https://example.com/calendar.ics")
        path_row.addWidget(self._source_edit, 1)
        self._browse_btn = QToolButton()
        self._browse_btn.setText("Browse…")
        self._browse_btn.clicked.connect(self._on_browse)
        self._browse_btn.setVisible(False)
        path_row.addWidget(self._browse_btn)
        self._source_label = QLabel("URL:")
        form.addRow(self._source_label, path_row)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("(auto-detected if blank)")
        form.addRow("Display name:", self._name_edit)

        color_row = QHBoxLayout()
        self._color_swatch = QToolButton()
        self._color_swatch.setFixedSize(28, 22)
        self._color_swatch.clicked.connect(self._on_pick_color)
        self._apply_swatch_color()
        color_row.addWidget(self._color_swatch)
        color_row.addStretch(1)
        form.addRow("Color:", color_row)

        outer.addLayout(form)

        # ── Status line ───────────────────────────────────────────────────
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        outer.addWidget(self._status_label)

        # ── Buttons ────────────────────────────────────────────────────────
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if self._ok_btn is not None:
            self._ok_btn.setText("Subscribe")
        self._buttons.accepted.connect(self._on_subscribe)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

        # ── Wire mode toggle ──────────────────────────────────────────────
        self._radio_web.toggled.connect(self._on_mode_changed)
        self._on_mode_changed()
        self._source_edit.textChanged.connect(self._update_ok_state)
        self._update_ok_state()

    def _on_mode_changed(self) -> None:
        if self._radio_web.isChecked():
            self._source_label.setText("URL:")
            self._source_edit.setPlaceholderText(
                "https://example.com/calendar.ics"
            )
            self._browse_btn.setVisible(False)
        else:
            self._source_label.setText("Path:")
            self._source_edit.setPlaceholderText("/path/to/calendar.ics")
            self._browse_btn.setVisible(True)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose .ics file", "", "iCalendar files (*.ics);;All files (*)"
        )
        if path:
            self._source_edit.setText(path)

    def _apply_swatch_color(self) -> None:
        border = QColor(self.color).darker(130).name()
        self._color_swatch.setStyleSheet(
            f"QToolButton {{ background-color: {self.color};"
            f" border: 2px solid {border}; border-radius: 4px; }}"
        )

    def _on_pick_color(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(self.color),
            self,
            "Choose calendar color",
            options=QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if chosen.isValid():
            self.color = chosen.name(QColor.NameFormat.HexRgb).lower()
            self._apply_swatch_color()

    def _update_ok_state(self) -> None:
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(bool(self._source_edit.text().strip()))

    def _set_busy(self, busy: bool) -> None:
        self._source_edit.setEnabled(not busy)
        self._name_edit.setEnabled(not busy)
        self._radio_web.setEnabled(not busy)
        self._radio_file.setEnabled(not busy)
        self._browse_btn.setEnabled(not busy)
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(not busy and bool(self._source_edit.text().strip()))

    def _on_subscribe(self) -> None:
        source = self._source_edit.text().strip()
        if not source:
            return

        try:
            canon = canonicalize_source(source)
        except ValueError as e:
            self._show_error(str(e))
            return

        self.canonical_source = canon
        self._status_label.setText("Fetching…")
        self._status_label.setStyleSheet("color: #888888;")
        self._set_busy(True)

        # Run the network/file IO in a background QThread; emit back to GUI.
        worker_thread = QThread(self)
        worker = _FetchWorker(canon)
        worker.moveToThread(worker_thread)
        worker_thread.started.connect(worker.run)
        # quit() must be queued before _on_fetch_done: the worker thread's
        # exec() loop needs to receive the quit event and exit while the GUI
        # thread is processing the result. If _on_fetch_done came first and
        # called wait(), the GUI thread would block before quit was delivered,
        # causing a deadlock.
        worker.finished.connect(worker_thread.quit)
        worker.finished.connect(self._on_fetch_done)
        worker.finished.connect(worker.deleteLater)
        worker_thread.finished.connect(worker_thread.deleteLater)
        self._worker_thread = worker_thread
        self._worker = worker
        worker_thread.start()

    def _on_fetch_done(self, result: object) -> None:
        # Worker + thread clean themselves up via the deleteLater chain wired
        # in _on_subscribe. Don't call wait() here — it would deadlock because
        # the queued quit() event can't be delivered while this slot runs.
        self._worker_thread = None
        self._worker = None

        if not isinstance(result, tuple) or len(result) != 5:
            self._show_error("unexpected internal error")
            return
        success, message, events, suggested, sha = result
        if not success:
            self._show_error(message)
            return

        if not events:
            self._show_error("no events found in this calendar")
            return

        name = (
            self._name_edit.text().strip()
            or suggested
            or self._derive_name_from_source()
        )
        if not name:
            name = "Subscription"
        self.display_name = name
        self.events = list(events)
        self.content_sha256 = sha
        self.accept()

    def _show_error(self, message: str) -> None:
        self._status_label.setText(f"⚠ {message}")
        self._status_label.setStyleSheet("color: #d97706;")
        self._set_busy(False)

    def _derive_name_from_source(self) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(self.canonical_source)
        if parsed.scheme == "file":
            tail = (parsed.path or "").rsplit("/", 1)[-1]
        else:
            tail = (parsed.path or parsed.netloc or "").rsplit("/", 1)[-1]
        return tail.removesuffix(".ics") or self.canonical_source

    def reject(self) -> None:  # type: ignore[override]
        # Cancel any pending worker so we don't leak threads.
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        super().reject()
