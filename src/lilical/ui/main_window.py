from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import override

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.ui.sidebar import Sidebar
from lilical.ui.tray import SystemTray
from lilical.ui.views.agenda import AgendaView
from lilical.ui.views.day import DayView
from lilical.ui.views.month import MonthView
from lilical.ui.views.week import WeekView
from lilical.ui.widgets.account_setup import AccountSetupDialog

log = logging.getLogger(__name__)

_VIEW_NAMES = ["Month", "Week", "Day", "Agenda"]
_DEFAULT_VIEW = "Week"


class _SyncStatusWidget(QWidget):
    """Compact coloured pill showing sync state in the status bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self._dot = QLabel("●")
        self._text = QLabel("Ready")
        self._dot.setStyleSheet("color: #6ee896;")
        layout.addWidget(self._dot)
        layout.addWidget(self._text)

    def set_syncing(self, label: str) -> None:
        self._dot.setStyleSheet("color: #9ec5ff;")
        self._text.setText(f"Syncing {label}…")

    def set_ok(self, label: str) -> None:
        self._dot.setStyleSheet("color: #6ee896;")
        self._text.setText(f"Synced {label}")

    def set_error(self, label: str, message: str) -> None:
        self._dot.setStyleSheet("color: #ff6b6b;")
        self._text.setText(f"⚠ {label}: {message}")
        self._text.setToolTip(message)

    def set_auth_expired(self, label: str, message: str = "") -> None:
        self._dot.setStyleSheet("color: #ff6b6b;")
        if message:
            # Keep the pill compact: show only the first line of the error.
            # Full message goes in the tooltip.
            first_line = message.strip().splitlines()[0] if message.strip() else ""
            text = f"🔑 {label}: Authentication failed — {first_line}"
            self._text.setText(text)
            self._text.setToolTip(message)
        else:
            self._text.setText(
                f"🔑 {label}: Authentication failed — right-click account to re-authenticate"
            )
            self._text.setToolTip("")

    def set_ready(self) -> None:
        self._dot.setStyleSheet("color: #6ee896;")
        self._text.setText("Ready")


class MainWindow(QMainWindow):
    def __init__(
        self, *, config, event_store, sync_engine, recurrence, secrets
    ) -> None:
        super().__init__()
        self._cfg = config
        self._store = event_store
        self._sync = sync_engine
        self._secrets = secrets
        self._current_view: QWidget | None = None
        self._view_actions: dict[str, QAction] = {}

        # Persistent prefs. QSettings reads/writes under the org/app names set
        # in app.py ("lilical"/"lilical") → ~/.config/lilical/lilical.conf.
        self._settings = QSettings()
        self._theme = str(self._settings.value("theme", "dark") or "dark")
        default_view = str(
            self._settings.value("default_view", _DEFAULT_VIEW) or _DEFAULT_VIEW
        )
        if default_view not in _VIEW_NAMES:
            default_view = _DEFAULT_VIEW
        self._default_view_name: str = default_view
        self._current_view_name: str = default_view

        self.setWindowTitle("lilical")
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # ── Sidebar ────────────────────────────────────────────────────────
        self._sidebar = Sidebar(event_store, add_account_callback=self._add_account)
        self._sidebar.rename_account_requested.connect(self._on_rename_account)
        self._sidebar.reauth_account_requested.connect(self._on_reauth_account)
        self._sidebar.sync_now_requested.connect(self._on_sync_now_account)
        self._sidebar.delete_account_requested.connect(self._on_delete_account)
        self._sidebar.calendar_visibility_changed.connect(
            self._on_calendar_visibility_changed
        )
        self._sidebar.calendar_color_changed.connect(self._on_calendar_color_changed)
        self._sidebar.date_selected.connect(self._on_sidebar_date_selected)
        main_layout.addWidget(self._sidebar)

        # ── Right side: toolbar + stacked views ───────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._toolbar = self._build_toolbar()
        right_layout.addWidget(self._toolbar)

        # Stacked widget (manual, no QStackedWidget, for direct access)
        self._view_container = QWidget()
        self._view_stack_layout = QVBoxLayout(self._view_container)
        self._view_stack_layout.setContentsMargins(0, 0, 0, 0)

        self._views: dict[str, QWidget] = {
            "Month": MonthView(event_store),
            "Week": WeekView(event_store),
            "Day": DayView(event_store),
            "Agenda": AgendaView(event_store),
        }
        for view in self._views.values():
            self._view_stack_layout.addWidget(view)
            view.hide()

        right_layout.addWidget(self._view_container, 1)
        main_layout.addWidget(right, 1)

        # ── Status bar ─────────────────────────────────────────────────────
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._sync_status = _SyncStatusWidget()
        self._statusbar.addPermanentWidget(self._sync_status)
        self._syncing_accounts: set[str] = set()

        # ── System tray ────────────────────────────────────────────────────
        self._tray = SystemTray(self)
        if QApplication.instance() and QApplication.instance().property("__tray_available") is not False:
            self._tray.show()

        # ── Theme ──────────────────────────────────────────────────────────
        self._apply_theme(self._theme)

        # ── Signal wiring ──────────────────────────────────────────────────
        self._store.events_changed.connect(self._on_events_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_progress.connect(self._on_sync_progress)
        self._sync.sync_finished.connect(self._on_sync_finished)
        self._sync.sync_failed.connect(self._on_sync_failed)
        self._sync.auth_expired.connect(self._on_auth_expired)

        # ── Keyboard shortcuts ─────────────────────────────────────────────
        self._setup_shortcuts()

        # ── Initial state ──────────────────────────────────────────────────
        self._switch_view(self._default_view_name)

        # Rebuild instances asynchronously to avoid first-launch freeze
        self._fire_async(self._rebuild_instances_async(), "rebuild_instances")

    # ── Toolbar ────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QToolBar:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)

        # Navigation buttons
        prev_btn = QToolButton()
        prev_btn.setText("‹")
        prev_btn.setToolTip("Previous period  (←)")
        prev_btn.clicked.connect(self._nav_prev)
        tb.addWidget(prev_btn)

        today_btn = QToolButton()
        today_btn.setText("Today")
        today_btn.setToolTip("Go to today  (T)")
        today_btn.clicked.connect(self._nav_today)
        tb.addWidget(today_btn)

        next_btn = QToolButton()
        next_btn.setText("›")
        next_btn.setToolTip("Next period  (→)")
        next_btn.clicked.connect(self._nav_next)
        tb.addWidget(next_btn)

        tb.addSeparator()

        # View switcher
        for name in _VIEW_NAMES:
            act = QAction(name, self)
            act.setCheckable(True)
            act.triggered.connect(lambda _, n=name: self._switch_view(n))
            tb.addAction(act)
            self._view_actions[name] = act

        tb.addSeparator()

        # Current range label (pushed to right by spacer)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tb.addWidget(spacer)

        self._range_label = QLabel()
        self._range_label.setStyleSheet("padding: 0 8px;")
        tb.addWidget(self._range_label)

        tb.addSeparator()

        # New event
        new_btn = QToolButton()
        new_btn.setText("✚")
        new_btn.setToolTip("New event  (N)")
        new_btn.clicked.connect(self._new_event)
        tb.addWidget(new_btn)

        # Quick add
        quick_btn = QToolButton()
        quick_btn.setText("⚡")
        quick_btn.setToolTip("Quick add  (Ctrl+Shift+A)")
        quick_btn.clicked.connect(self._quick_add)
        tb.addWidget(quick_btn)

        # Refresh
        refresh_btn = QToolButton()
        refresh_btn.setText("⟳")
        refresh_btn.setToolTip("Refresh now  (Ctrl+R)")
        refresh_btn.clicked.connect(self._refresh_all)
        tb.addWidget(refresh_btn)

        # Preferences
        prefs_btn = QToolButton()
        prefs_btn.setText("⚙")
        prefs_btn.setToolTip("Preferences  (Ctrl+,)")
        prefs_btn.clicked.connect(self._open_preferences)
        tb.addWidget(prefs_btn)

        return tb

    def _update_range_label(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "range_label"):
            self._range_label.setText(view.range_label())

    # ── Keyboard shortcuts ─────────────────────────────────────────────────

    def _setup_shortcuts(self) -> None:
        def sc(key: str, fn) -> None:
            QShortcut(QKeySequence(key), self).activated.connect(fn)

        # View switching
        sc("1", lambda: self._switch_view("Month"))
        sc("2", lambda: self._switch_view("Week"))
        sc("3", lambda: self._switch_view("Day"))
        sc("4", lambda: self._switch_view("Agenda"))

        # Navigation
        sc("t", self._nav_today)
        sc("Left", self._nav_prev)
        sc("Right", self._nav_next)
        sc("PgUp", self._nav_prev)
        sc("PgDown", self._nav_next)

        # New event
        sc("n", self._new_event)
        sc("Ctrl+N", self._new_event)

        # Quick add
        sc("Ctrl+Shift+A", self._quick_add)

        # Sync
        sc("Ctrl+R", self._refresh_all)

        # Preferences
        sc("Ctrl+,", self._open_preferences)

        # Full-screen
        sc("F11", self._toggle_fullscreen)

        # Zoom (Week/Day)
        sc("Ctrl++", self._zoom_in)
        sc("Ctrl+-", self._zoom_out)
        sc("Ctrl+0", self._zoom_reset)

        # Escape: close any open dialog (Qt dialogs handle this; also hide if minimised)
        sc("Escape", self._on_escape)

        # Help overlay (placeholder — just show a message for now)
        sc("?", self._show_shortcut_help)

    # ── Navigation ─────────────────────────────────────────────────────────

    def _nav_prev(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "navigate"):
            view.navigate(-1)
            self._update_range_label()

    def _nav_next(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "navigate"):
            view.navigate(1)
            self._update_range_label()

    def _nav_today(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "go_today"):
            view.go_today()
            self._update_range_label()

    def _on_sidebar_date_selected(self, d) -> None:
        """Jump to the clicked date in mini-month: show Day view for that date."""
        from datetime import date
        day_view = self._views.get("Day")
        if isinstance(day_view, DayView):
            day_view._day = d
            from lilical.ui.views.day import DayGrid
            day_view._scene.removeItem(day_view._grid)
            day_view._grid = DayGrid(d, max(800, day_view.viewport().width()))
            day_view._scene.addItem(day_view._grid)
            day_view._scene.setSceneRect(day_view._grid.boundingRect())
            day_view.refresh()
        self._switch_view("Day")

    # ── View switching ─────────────────────────────────────────────────────

    def _switch_view(self, name: str) -> None:
        if self._current_view is not None:
            self._current_view.hide()
        self._current_view_name = name
        view = self._views[name]
        self._current_view = view
        view.show()
        if hasattr(view, "refresh"):
            view.refresh()
        # Update toolbar checkmarks
        for n, act in self._view_actions.items():
            act.setChecked(n == name)
        self._update_range_label()

    # ── Events ─────────────────────────────────────────────────────────────

    def _new_event(self) -> None:
        from lilical.ui.widgets.event_dialog import EventDialog

        dlg = EventDialog(self, store=self._store)
        if dlg.exec() == QDialog.Accepted:
            cal_id = dlg.calendar_id
            if not cal_id:
                QMessageBox.warning(self, "No calendar", "Please add an account first.")
                return
            event = dlg.build_event(uid=str(uuid.uuid4()))
            try:
                self._store.queue_create(event)
            except Exception:
                log.exception("Failed to create event")
                QMessageBox.critical(self, "Error", "Failed to save event.")

    def _quick_add(self) -> None:
        from lilical.ui.widgets.quick_add_dialog import QuickAddDialog

        dlg = QuickAddDialog(self, store=self._store)
        dlg.exec()

    # ── Sync actions ───────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        for acc in self._store.list_accounts():
            self._sync.force_refresh(acc.id)
        self._sync_status.set_syncing("all accounts")

    # ── Preferences ────────────────────────────────────────────────────────

    def _open_preferences(self) -> None:
        from lilical.ui.widgets.preferences_dialog import PreferencesDialog

        dlg = PreferencesDialog(
            self,
            current_theme=self._theme,
            current_default_view=self._default_view_name,
        )
        if dlg.exec() == QDialog.Accepted:
            if dlg.theme != self._theme:
                self._theme = dlg.theme
                self._apply_theme(self._theme)
                self._settings.setValue("theme", self._theme)
            if dlg.default_view != self._default_view_name and dlg.default_view in _VIEW_NAMES:
                self._default_view_name = dlg.default_view
                self._settings.setValue("default_view", self._default_view_name)

    def _apply_theme(self, name: str) -> None:
        try:
            theme_path = Path(__file__).parent / "styles" / f"{name}.qss"
            if theme_path.exists():
                with open(theme_path) as f:
                    self.setStyleSheet(f.read())
            else:
                log.warning("Theme file not found: %s", theme_path)
        except Exception:
            log.exception("Failed to apply theme '%s'", name)

    # ── Zoom (placeholder) ────────────────────────────────────────────────

    def _zoom_in(self) -> None:
        pass  # Week/Day zoom not yet implemented

    def _zoom_out(self) -> None:
        pass

    def _zoom_reset(self) -> None:
        pass

    # ── Full-screen / escape ──────────────────────────────────────────────

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_escape(self) -> None:
        # If full-screen, exit it; otherwise ignore (Qt dialogs handle their own Esc)
        if self.isFullScreen():
            self.showNormal()

    # ── Shortcut help overlay (minimal) ──────────────────────────────────

    def _show_shortcut_help(self) -> None:
        help_text = (
            "Keyboard shortcuts\n"
            "──────────────────\n"
            "1–4       Switch view (Month/Week/Day/Agenda)\n"
            "T         Go to today\n"
            "← / →    Previous / next period\n"
            "N         New event\n"
            "Ctrl+N    New event\n"
            "Ctrl+Shift+A  Quick add\n"
            "Ctrl+R    Refresh now\n"
            "Ctrl+,    Preferences\n"
            "F11       Toggle full-screen\n"
            "?         This help\n"
        )
        QMessageBox.information(self, "Keyboard shortcuts", help_text)

    # ── Async helpers ─────────────────────────────────────────────────────

    def _fire_async(self, coro, label: str) -> asyncio.Task:
        """Schedule a coroutine and log any exception it raises."""
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: self._on_task_done(t, label))
        return task

    @staticmethod
    def _on_task_done(task: asyncio.Task, label: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Async task '%s' failed: %s", label, exc, exc_info=exc)

    async def _rebuild_instances_async(self) -> None:
        await asyncio.to_thread(self._store.rebuild_all_instances)

    # ── Sync signal handlers ──────────────────────────────────────────────

    def _account_label(self, account_id: str) -> str:
        acc = self._store.get_account(account_id)
        return acc.display_name if acc is not None else account_id

    def _on_sync_started(self, account_id: str) -> None:
        self._syncing_accounts.add(account_id)
        self._sync_status.set_syncing(self._account_label(account_id))

    def _on_sync_progress(self, account_id: str, calendar_label: str, count: int) -> None:
        label = f"{self._account_label(account_id)} / {calendar_label}"
        self._sync_status.set_syncing(f"{label} ({count} events)")

    def _on_sync_finished(self, account_id: str, n_changes: int) -> None:
        self._syncing_accounts.discard(account_id)
        label = self._account_label(account_id)
        self._sync_status.set_ok(f"{label} ({n_changes} changes)")
        self._sidebar.refresh()

    def _on_sync_failed(self, account_id: str, message: str) -> None:
        self._syncing_accounts.discard(account_id)
        label = self._account_label(account_id)
        # Persistent — does NOT auto-dismiss
        self._sync_status.set_error(label, message)
        log.error("Sync failed for %s: %s", label, message)

    def _on_auth_expired(self, account_id: str, message: str = "") -> None:
        # No modal dialog here: this slot can fire mid-await of another sync task.
        # Show as persistent status instead, including the underlying error so
        # the user can act on it (wrong password vs MFA vs wrong URL etc.).
        self._syncing_accounts.discard(account_id)
        label = self._account_label(account_id)
        self._sync_status.set_auth_expired(label, message)
        log.warning("Auth failed for account %s (%s): %s", account_id, label, message)

    def _on_events_changed(self, calendar_id: str, uids: set) -> None:
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()
        self._update_range_label()

    # ── Account management ────────────────────────────────────────────────

    def _add_account(self) -> None:
        dlg = AccountSetupDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.result_data()
        if data is None:
            return
        kind, display_name, identity, server_url, secret_data = data
        account_id = str(uuid.uuid4())
        calendar_id = str(uuid.uuid4())
        self._secrets.set(account_id, secret_data)
        self._store.create_account(
            account_id=account_id,
            kind=kind,
            display_name=display_name,
            identity=identity,
            server_url=server_url,
            calendar_id=calendar_id,
            calendar_display_name=display_name or identity or "Calendar",
        )
        self._sidebar.refresh()
        self._fire_async(self._sync.start_account(account_id), f"start_account/{account_id}")

    def _on_rename_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename account",
            "Display name:",
            QLineEdit.EchoMode.Normal,
            acc.display_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == acc.display_name:
            return
        self._store.update_account(account_id, display_name=new_name)
        self._sidebar.refresh()

    def _on_reauth_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        dlg = AccountSetupDialog(self, existing_account=acc)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.result_data()
        if data is None:
            return
        _kind, display_name, identity, server_url, secret_data = data
        # Only persist secrets if the user actually entered new values. An
        # empty dict (or a dict with only empty values) means "keep the
        # existing secret" — never overwrite a working credential with "".
        if secret_data and any(v for v in secret_data.values()):
            self._secrets.set(account_id, secret_data)
        self._store.update_account(
            account_id,
            display_name=display_name,
            identity=identity,
            server_url=server_url,
        )
        self._sidebar.refresh()
        self._fire_async(self._restart_account_sync(account_id), f"restart_sync/{account_id}")
        # Clear any auth-expired warning
        self._sync_status.set_ready()

    def _on_sync_now_account(self, account_id: str) -> None:
        self._sync.force_refresh(account_id)
        self._sync_status.set_syncing(self._account_label(account_id))

    def _on_delete_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        cals = self._store.list_calendars(account_id, visible_only=False)
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete account?")
        confirm.setText(f'Delete account "{acc.display_name}"?')
        confirm.setInformativeText(
            f"This will remove {len(cals)} calendar(s) and all locally-cached "
            "events for this account. Events stored on the provider's servers "
            "will not be affected."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        self._fire_async(self._teardown_account(account_id), f"teardown/{account_id}")

    async def _teardown_account(self, account_id: str) -> None:
        await self._sync.stop_account(account_id)
        self._secrets.delete(account_id)
        await asyncio.to_thread(self._store.delete_account, account_id)
        self._sidebar.refresh()
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()
        self._sync_status.set_ready()

    async def _restart_account_sync(self, account_id: str) -> None:
        await self._sync.stop_account(account_id)
        await self._sync.start_account(account_id)

    def _on_calendar_visibility_changed(self, calendar_id: str, is_visible: bool) -> None:
        try:
            self._store.set_calendar_visibility(calendar_id, is_visible)
        except Exception:
            log.exception("Failed to update calendar visibility for %s", calendar_id)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()

    def _on_calendar_color_changed(self, _calendar_id: str, _new_hex: str) -> None:
        # The swatch already persisted via store.set_calendar_color. Just kick
        # the current view to re-paint its chips with the new fallback color.
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()

    # ── Window lifecycle ──────────────────────────────────────────────────

    @override
    def closeEvent(self, e) -> None:
        # Closing the window quits the app. Use the tray icon's Show/Quit to
        # keep it running in the background instead.
        super().closeEvent(e)
        QApplication.quit()
