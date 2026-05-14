from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore


class Sidebar(QWidget):
    rename_account_requested = Signal(str)
    reauth_account_requested = Signal(str)
    sync_now_requested = Signal(str)
    delete_account_requested = Signal(str)
    calendar_visibility_changed = Signal(str, bool)

    def __init__(
        self,
        store: EventStore,
        add_account_callback=None,
    ) -> None:
        super().__init__()
        self._store = store
        self._add_account_callback = add_account_callback
        self.setFixedWidth(240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("Calendars")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_widget = QWidget()
        self._cal_layout = QVBoxLayout(self._scroll_widget)
        self._cal_layout.setContentsMargins(0, 0, 0, 0)
        self._cal_layout.setSpacing(2)
        self._cal_layout.addStretch(1)
        scroll.setWidget(self._scroll_widget)
        layout.addWidget(scroll)

        add_btn = QPushButton("+ Add account")
        if add_account_callback is not None:
            add_btn.clicked.connect(add_account_callback)
        layout.addWidget(add_btn)

        self._checkboxes: dict[str, QCheckBox] = {}
        self._account_widgets: list[QWidget] = []
        self.refresh()

    def refresh(self) -> None:
        for w in self._account_widgets:
            w.setParent(None)
            w.deleteLater()
        self._account_widgets.clear()
        self._checkboxes.clear()

        # Stretch item is always at the end; insert account groups before it.
        insert_at = max(self._cal_layout.count() - 1, 0)

        for acc in self._store.list_accounts():
            group = self._build_account_group(acc)
            self._cal_layout.insertWidget(insert_at, group)
            insert_at += 1
            self._account_widgets.append(group)

    def _build_account_group(self, account) -> QWidget:
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(2)

        # Header row: account name + ⋯ menu button
        header = QWidget()
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        label = QLabel(account.display_name)
        label.setStyleSheet("font-weight: bold;")
        label.setToolTip(f"{account.identity} ({account.kind})")
        h.addWidget(label, 1)

        menu_btn = QToolButton()
        menu_btn.setText("⋯")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        menu_btn.setAutoRaise(True)
        menu = QMenu(menu_btn)
        account_id = account.id

        def _emit(signal):
            return lambda _checked=False, aid=account_id: signal.emit(aid)

        act_rename = QAction("Rename…", menu)
        act_rename.triggered.connect(_emit(self.rename_account_requested))
        menu.addAction(act_rename)

        act_reauth = QAction("Re-authenticate…", menu)
        act_reauth.triggered.connect(_emit(self.reauth_account_requested))
        menu.addAction(act_reauth)

        act_sync = QAction("Sync now", menu)
        act_sync.triggered.connect(_emit(self.sync_now_requested))
        menu.addAction(act_sync)

        menu.addSeparator()

        act_delete = QAction("Delete account…", menu)
        act_delete.triggered.connect(_emit(self.delete_account_requested))
        menu.addAction(act_delete)

        menu_btn.setMenu(menu)
        h.addWidget(menu_btn)
        v.addWidget(header)

        # Calendar checkboxes
        cals = self._store.list_calendars(account.id, visible_only=False)
        for cal in cals:
            cb = QCheckBox(cal.display_name)
            cb.setChecked(bool(cal.is_visible))
            cb.setContentsMargins(12, 0, 0, 0)
            cb.toggled.connect(
                lambda checked, cid=cal.id: self.calendar_visibility_changed.emit(
                    cid, bool(checked)
                )
            )
            v.addWidget(cb)
            self._checkboxes[cal.id] = cb

        if not cals:
            placeholder = QLabel("(no calendars yet)")
            placeholder.setStyleSheet("color: #888; padding-left: 12px;")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignLeft)
            v.addWidget(placeholder)

        return container
