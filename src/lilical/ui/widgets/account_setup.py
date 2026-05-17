from __future__ import annotations

import concurrent.futures
import contextlib
import webbrowser
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)

_KIND_TO_LABEL = {
    "google": "Google Calendar",
    "graph": "Microsoft / Outlook",
    "caldav": "CalDAV",
}
_LABEL_TO_KIND = {v: k for k, v in _KIND_TO_LABEL.items()}


class AccountSetupDialog(QDialog):
    _OAUTH_TIMEOUT = 300

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        existing_account=None,
    ) -> None:
        super().__init__(parent)
        self._existing_account = existing_account
        self._reauth = existing_account is not None

        if self._reauth:
            if existing_account is None:
                return
            self.setWindowTitle(f"Re-authenticate — {existing_account.display_name}")
        else:
            self.setWindowTitle("Add an account")
        self.setMinimumWidth(420)

        self._secret_data: dict[str, Any] = {}
        self._chosen_include_contacts: bool = False
        self._active_oauth_pool: concurrent.futures.ThreadPoolExecutor | None = None

        layout = QVBoxLayout(self)

        if self._reauth:
            layout.addWidget(
                QLabel(
                    "Refresh credentials for this account. You can also update "
                    "its display name, identity, and server URL here."
                )
            )
        else:
            layout.addWidget(QLabel("Step 1 of 3 — What kind of account?"))

        self._kind_combo = QComboBox()
        self._kind_combo.addItems(["Google Calendar", "Microsoft / Outlook", "CalDAV"])
        if self._reauth:
            if existing_account is None:
                return
            label = _KIND_TO_LABEL.get(existing_account.kind)
            if label is not None:
                self._kind_combo.setCurrentText(label)
            self._kind_combo.setEnabled(False)
        layout.addWidget(self._kind_combo)

        self._form = QFormLayout()
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Work, Personal")
        self._form.addRow("Display name:", self._name_edit)

        self._identity_edit = QLineEdit()
        self._identity_edit.setPlaceholderText("email@example.com")
        self._form.addRow("Email / Username:", self._identity_edit)

        self._server_edit = QLineEdit()
        self._server_edit.setPlaceholderText("https://caldav.example.com")
        self._form.addRow("Server URL:", self._server_edit)

        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText("App password or password")
        self._form.addRow("Password:", self._password_edit)

        layout.addLayout(self._form)
        self._server_edit.setVisible(False)
        self._password_edit.setVisible(False)

        if self._reauth:
            if existing_account is None:
                return
            self._name_edit.setText(existing_account.display_name or "")
            self._identity_edit.setText(existing_account.identity or "")
            if existing_account.server_url:
                self._server_edit.setText(existing_account.server_url)

        self._contacts_checkbox = QCheckBox(
            "Include contacts from this account"
            " — may require admin approval on some tenants"
        )
        self._contacts_checkbox.setToolTip(
            "Off (recommended): calendar only."
            " On: imports your address book and organization\n"
            "directory for invite autocomplete."
            " lilical always autocompletes from people you've\n"
            "previously emailed, regardless of this setting."
        )
        layout.addWidget(self._contacts_checkbox)

        self._kind_combo.currentTextChanged.connect(self._on_kind_changed)
        self._on_kind_changed(self._kind_combo.currentText())

        if self._reauth and existing_account is not None:
            self._contacts_checkbox.setChecked(bool(existing_account.include_contacts))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self._continue_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._continue_btn.setText("Re-authenticate" if self._reauth else "Continue")
        buttons.accepted.connect(self._on_continue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_kind_changed(self, text: str) -> None:
        is_caldav = text == "CalDAV"
        is_graph = text == "Microsoft / Outlook"
        self._server_edit.setVisible(is_caldav)
        self._password_edit.setVisible(is_caldav)
        self._identity_edit.setPlaceholderText(
            "Username" if is_caldav else "email@example.com"
        )
        if not is_caldav:
            self._server_edit.clear()
            self._password_edit.clear()
        self._contacts_checkbox.setVisible(is_graph)

    def _collect_data(
        self,
    ) -> tuple[str | None, str, str, str | None, dict[str, Any]] | None:
        kind_map = {
            "Google Calendar": "google",
            "Microsoft / Outlook": "graph",
            "CalDAV": "caldav",
        }
        kind = kind_map.get(self._kind_combo.currentText())
        display_name = (
            self._name_edit.text().strip() or self._identity_edit.text().strip()
        )
        identity = self._identity_edit.text().strip()
        server_url = self._server_edit.text().strip() or None

        if not identity:
            return None

        secret_data: dict[str, Any] = {}
        if kind == "caldav":
            # Only include the password if non-empty. On re-auth the field is
            # blank by default to mean "keep the existing password"; persisting
            # "" here would silently wipe a working secret.
            pw = self._password_edit.text()
            if pw:
                secret_data["password"] = pw

        return (kind, display_name, identity, server_url, secret_data)

    def _on_continue(self) -> None:
        data = self._collect_data()
        if data is None:
            QMessageBox.warning(
                self, "Missing info", "Please enter an email or username."
            )
            return

        kind, _display_name, _identity, _server_url, secret_data = data

        include_contacts = self._contacts_checkbox.isChecked() and kind == "graph"

        if kind == "google":
            token = self._run_google_browser_flow()
            if token is None:
                return
            secret_data["token"] = token
        elif kind == "graph":
            cache_json = self._run_graph_device_flow(include_contacts=include_contacts)
            if cache_json is None:
                return
            secret_data["msal_cache"] = cache_json

        self._secret_data = secret_data
        self._chosen_include_contacts = include_contacts
        self.accept()

    def _cancel_active_oauth(self) -> None:
        if self._active_oauth_pool is not None:
            self._active_oauth_pool.shutdown(wait=False, cancel_futures=True)
            self._active_oauth_pool = None

    def _run_graph_device_flow(self, *, include_contacts: bool = False) -> str | None:
        import importlib.util as _util

        if _util.find_spec("msal") is None:
            QMessageBox.critical(
                self,
                "Authentication failed",
                "Microsoft 365 support requires the 'msal' package. "
                "Install it with: pixi install",
            )
            return None

        from lilical.backends.graph import (
            complete_graph_device_flow,
            initiate_graph_device_flow,
        )

        try:
            app, cache, flow = initiate_graph_device_flow(
                include_contacts=include_contacts
            )
        except Exception as e:
            QMessageBox.critical(self, "Authentication failed", str(e))
            return None

        user_code: str = str(flow.get("user_code", ""))
        verification_uri = flow.get(
            "verification_uri", "https://microsoft.com/devicelogin"
        )
        with contextlib.suppress(Exception):
            webbrowser.open(str(verification_uri))

        dialog = QDialog(self)
        dialog.setWindowTitle("Sign in to Microsoft 365")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)

        layout.addWidget(
            QLabel(
                "A browser tab is opening Microsoft's sign-in page.\n"
                "Enter this code when prompted:"
            )
        )
        code_label = QLabel(user_code)
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_label.setStyleSheet(
            "font-size: 28pt; font-weight: bold; letter-spacing: 6px; "
            "padding: 16px; background: palette(base); border-radius: 6px;"
        )
        code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(code_label)

        url_label = QLabel(
            f'<a href="{verification_uri}">{verification_uri}</a> '
            "(if the browser did not open automatically)"
        )
        url_label.setOpenExternalLinks(True)
        layout.addWidget(url_label)

        status_label = QLabel("Waiting for sign-in...")
        layout.addWidget(status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        self._cancel_active_oauth()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._active_oauth_pool = pool
        future = pool.submit(complete_graph_device_flow, app, cache, flow)

        result: dict[str, Any] = {"token": None, "error": None}

        def _check_done() -> None:
            if future.done():
                try:
                    result["token"] = future.result()
                except Exception as exc:
                    result["error"] = exc
                dialog.accept()

        timer = QTimer(dialog)
        timer.timeout.connect(_check_done)
        timer.start(500)

        try:
            outcome = dialog.exec()
        finally:
            timer.stop()
            pool.shutdown(wait=False, cancel_futures=True)
            self._active_oauth_pool = None

        if outcome != QDialog.DialogCode.Accepted:
            return None
        if result["error"] is not None:
            QMessageBox.critical(self, "Authentication failed", str(result["error"]))
            return None
        return result["token"]

    def _run_google_browser_flow(self) -> str | None:
        import importlib.util as _util

        if _util.find_spec("google_auth_oauthlib") is None:
            QMessageBox.critical(
                self,
                "Authentication failed",
                "Google Calendar support requires 'google-auth-oauthlib'. "
                "Install it with: pixi install",
            )
            return None

        from lilical.backends.google import run_google_oauth_sync

        progress = QProgressDialog(
            "Opening browser for Google sign-in…\n"
            "Complete the sign-in in your browser, then return here.",
            "Cancel",
            0,
            0,
            self,
        )
        progress.setWindowTitle("Sign in to Google")
        progress.setMinimumDuration(0)
        progress.show()

        self._cancel_active_oauth()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._active_oauth_pool = pool
        try:
            future = pool.submit(run_google_oauth_sync)
            while not future.done():
                QApplication.processEvents()
                if progress.wasCanceled():
                    return None
                with contextlib.suppress(concurrent.futures.TimeoutError):
                    future.result(timeout=0.2)
            return future.result()
        except Exception as e:
            QMessageBox.critical(self, "Authentication failed", str(e))
            return None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            self._active_oauth_pool = None
            progress.close()

    def result_data(
        self,
    ) -> tuple[str | None, str, str, str | None, dict[str, Any], bool] | None:
        kind_map = {
            "Google Calendar": "google",
            "Microsoft / Outlook": "graph",
            "CalDAV": "caldav",
        }
        kind = kind_map.get(self._kind_combo.currentText())
        display_name = (
            self._name_edit.text().strip() or self._identity_edit.text().strip()
        )
        identity = self._identity_edit.text().strip()
        server_url = self._server_edit.text().strip() or None

        if not identity:
            return None

        return (
            kind,
            display_name,
            identity,
            server_url,
            dict(self._secret_data),
            self._chosen_include_contacts,
        )
