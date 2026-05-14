from __future__ import annotations

import asyncio
import uuid
from typing import override

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from lilical.ui.sidebar import Sidebar
from lilical.ui.tray import SystemTray
from lilical.ui.views.agenda import AgendaView
from lilical.ui.views.day import DayView
from lilical.ui.views.month import MonthView
from lilical.ui.views.week import WeekView
from lilical.ui.views.year import YearView
from lilical.ui.widgets.account_setup import AccountSetupDialog


class MainWindow(QMainWindow):
    def __init__(
        self, *, config, event_store, sync_engine, recurrence, secrets
    ) -> None:
        super().__init__()
        self._cfg = config
        self._store = event_store
        self._sync = sync_engine
        self._secrets = secrets
        self._current_view = None

        self.setWindowTitle("lilical")
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # Sidebar
        self._sidebar = Sidebar(event_store, add_account_callback=self._add_account)
        self._sidebar.rename_account_requested.connect(self._on_rename_account)
        self._sidebar.reauth_account_requested.connect(self._on_reauth_account)
        self._sidebar.sync_now_requested.connect(self._on_sync_now_account)
        self._sidebar.delete_account_requested.connect(self._on_delete_account)
        self._sidebar.calendar_visibility_changed.connect(
            self._on_calendar_visibility_changed
        )
        main_layout.addWidget(self._sidebar)

        # Right side: toolbar + stacked views
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._toolbar = QToolBar()
        right_layout.addWidget(self._toolbar)

        self._views: dict[str, QWidget] = {}
        views = [
            ("Month", MonthView(event_store)),
            ("Week", WeekView(event_store)),
            ("Day", DayView(event_store)),
            ("Year", YearView(event_store)),
            ("Agenda", AgendaView(event_store)),
        ]
        self._stack = QStackedWidget()
        for name, view in views:
            self._stack.addWidget(view)
            self._views[name] = view
            act = QAction(name, self)
            act.setCheckable(True)
            act.triggered.connect(lambda _, v=view, n=name: self._switch_view(n, v))
            self._toolbar.addAction(act)

        right_layout.addWidget(self._stack)
        main_layout.addWidget(right)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

        # Indeterminate progress bar parked at the right of the status bar.
        # Graph delta sync doesn't return a total event count, so we run it
        # in busy mode and rely on the status message for the running count.
        self._syncing_accounts: set[str] = set()
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setMaximumWidth(160)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.hide()
        self._status.addPermanentWidget(self._progress_bar)

        # System tray
        self._tray = SystemTray(self)
        self._tray.show()

        # Dark theme default
        self._apply_theme("dark")

        self._store.events_changed.connect(self._on_events_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_progress.connect(self._on_sync_progress)
        self._sync.sync_finished.connect(self._on_sync_finished)
        self._sync.sync_failed.connect(self._on_sync_failed)
        self._sync.auth_expired.connect(self._on_auth_expired)

        # Rebuild instances off the UI thread to avoid first-launch freeze
        asyncio.ensure_future(self._rebuild_instances_async())

        # Activate first view
        if views:
            self._switch_view(views[0][0], views[0][1])

    async def _rebuild_instances_async(self) -> None:
        await asyncio.to_thread(self._store.rebuild_all_instances)

    def _switch_view(self, name: str, view: QWidget) -> None:
        self._current_view = view
        self._stack.setCurrentWidget(view)
        if hasattr(view, "refresh"):
            view.refresh()

    def _apply_theme(self, name: str) -> None:
        try:
            from pathlib import Path

            theme_path = Path(__file__).parent / "styles" / f"{name}.qss"
            if theme_path.exists():
                with open(theme_path) as f:
                    self.setStyleSheet(f.read())
        except Exception:
            pass

    def _on_events_changed(self, calendar_id: str, uids: set) -> None:
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()

    def _account_label(self, account_id: str) -> str:
        acc = self._store.get_account(account_id)
        return acc.display_name if acc is not None else account_id

    def _set_account_syncing(self, account_id: str, syncing: bool) -> None:
        if syncing:
            self._syncing_accounts.add(account_id)
        else:
            self._syncing_accounts.discard(account_id)
        self._progress_bar.setVisible(bool(self._syncing_accounts))

    def _on_sync_started(self, account_id: str) -> None:
        self._set_account_syncing(account_id, True)
        self._status.showMessage(f"Syncing {self._account_label(account_id)}...")

    def _on_sync_progress(
        self, account_id: str, calendar_label: str, count: int
    ) -> None:
        self._status.showMessage(
            f"Syncing {self._account_label(account_id)} / "
            f"{calendar_label}: {count} events..."
        )

    def _on_sync_finished(self, account_id: str, n_changes: int) -> None:
        self._set_account_syncing(account_id, False)
        self._status.showMessage(
            f"Synced {self._account_label(account_id)} ({n_changes} changes)", 5000
        )
        self._sidebar.refresh()

    def _on_sync_failed(self, account_id: str, message: str) -> None:
        self._set_account_syncing(account_id, False)
        self._status.showMessage(
            f"Sync failed for {self._account_label(account_id)}: {message}", 5000
        )

    def _on_auth_expired(self, account_id: str) -> None:
        # No modal dialog here: this slot can fire while another sync task is
        # mid-await, and a modal would re-enter the qasync event loop and
        # also block any pending stop_account/delete teardown.
        self._set_account_syncing(account_id, False)
        label = self._account_label(account_id)
        self._status.showMessage(
            f"Authentication expired for {label} — right-click the account "
            "to re-authenticate, or delete it."
        )

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
        asyncio.ensure_future(self._sync.start_account(account_id))

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
        if secret_data:
            self._secrets.set(account_id, secret_data)
        self._store.update_account(
            account_id,
            display_name=display_name,
            identity=identity,
            server_url=server_url,
        )
        self._sidebar.refresh()
        asyncio.ensure_future(self._restart_account_sync(account_id))

    def _on_sync_now_account(self, account_id: str) -> None:
        self._sync.force_refresh(account_id)
        self._status.showMessage(
            f"Sync requested for {self._account_label(account_id)}", 3000
        )

    def _on_delete_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        cals = self._store.list_calendars(account_id, visible_only=False)
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete account?")
        confirm.setText(f"Delete account “{acc.display_name}”?")
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
        asyncio.ensure_future(self._teardown_account(account_id))

    async def _teardown_account(self, account_id: str) -> None:
        await self._sync.stop_account(account_id)
        self._secrets.delete(account_id)
        await asyncio.to_thread(self._store.delete_account, account_id)
        self._sidebar.refresh()
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()
        self._status.showMessage("Account deleted", 3000)

    async def _restart_account_sync(self, account_id: str) -> None:
        await self._sync.stop_account(account_id)
        await self._sync.start_account(account_id)

    def _on_calendar_visibility_changed(
        self, calendar_id: str, is_visible: bool
    ) -> None:
        self._store.set_calendar_visibility(calendar_id, is_visible)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()

    @override
    def closeEvent(self, e):
        super().closeEvent(e)
