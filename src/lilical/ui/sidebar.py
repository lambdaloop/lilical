from __future__ import annotations

from datetime import date
from functools import partial

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
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
from lilical.ui.widgets.mini_month import MiniMonthGrid


class _CalendarSwatch(QToolButton):
    """A small color square next to each calendar; click to pick a new color."""

    color_changed = Signal(str, str)  # calendar_id, new_hex

    def __init__(self, calendar_id: str, color_hex: str, store: EventStore) -> None:
        super().__init__()
        self._calendar_id = calendar_id
        self._color = color_hex
        self._store = store
        self.setFixedSize(14, 14)
        self.setToolTip("Click to change calendar color")
        self.setAutoRaise(True)
        self._apply_style()
        self.clicked.connect(self._on_clicked)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"QToolButton {{"
            f"  background-color: {self._color};"
            f"  border: 1px solid #7a7a7a;"
            f"  border-radius: 3px;"
            f"}}"
            f"QToolButton:hover {{ border-color: #ffffff; }}"
        )

    def _on_clicked(self) -> None:
        initial = QColor(self._color)
        if not initial.isValid():
            initial = QColor("#5e9fff")
        chosen = QColorDialog.getColor(
            initial,
            self,
            "Choose calendar color",
            options=QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if not chosen.isValid():
            return
        new_hex = chosen.name(QColor.NameFormat.HexRgb).lower()
        if new_hex == self._color.lower():
            return
        self._color = new_hex
        self._apply_style()
        self._store.set_calendar_color(self._calendar_id, new_hex)
        self.color_changed.emit(self._calendar_id, new_hex)


class Sidebar(QWidget):
    rename_account_requested = Signal(str)
    reauth_account_requested = Signal(str)
    sync_now_requested = Signal(str)
    delete_account_requested = Signal(str)
    calendar_visibility_changed = Signal(str, bool)
    calendar_color_changed = Signal(str, str)  # calendar_id, new_hex
    date_selected = Signal(date)  # from mini-month

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
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── Mini-month picker ──────────────────────────────────────────────
        mini_header = QHBoxLayout()
        self._mini_prev = QToolButton()
        self._mini_prev.setText("◀")
        self._mini_prev.setAutoRaise(True)
        self._mini_label = QLabel()
        self._mini_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mini_next = QToolButton()
        self._mini_next.setText("▶")
        self._mini_next.setAutoRaise(True)
        mini_header.addWidget(self._mini_prev)
        mini_header.addWidget(self._mini_label, 1)
        mini_header.addWidget(self._mini_next)
        layout.addLayout(mini_header)

        self._mini_month = MiniMonthGrid()
        layout.addWidget(self._mini_month)
        self._update_mini_label()

        self._mini_prev.clicked.connect(self._on_mini_prev)
        self._mini_next.clicked.connect(self._on_mini_next)
        self._mini_month.selected.connect(self.date_selected)

        # ── Divider ────────────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: #555555;")
        layout.addWidget(div)

        # ── Calendars label ────────────────────────────────────────────────
        cal_title = QLabel("Calendars")
        cal_title.setStyleSheet("font-weight: bold; padding: 2px 4px;")
        layout.addWidget(cal_title)

        # ── Calendar list (scrollable) ─────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_widget = QWidget()
        self._cal_layout = QVBoxLayout(self._scroll_widget)
        self._cal_layout.setContentsMargins(0, 0, 0, 0)
        self._cal_layout.setSpacing(2)
        self._cal_layout.addStretch(1)
        scroll.setWidget(self._scroll_widget)
        layout.addWidget(scroll, 1)

        add_btn = QPushButton("+ Add account")
        if add_account_callback is not None:
            add_btn.clicked.connect(add_account_callback)
        layout.addWidget(add_btn)

        self._checkboxes: dict[str, QCheckBox] = {}
        self._account_widgets: list[QWidget] = []
        self.refresh()

    # ── Mini-month navigation ──────────────────────────────────────────────

    def _update_mini_label(self) -> None:
        yr = self._mini_month.year
        mo = self._mini_month.month
        self._mini_label.setText(date(yr, mo, 1).strftime("%B %Y"))

    def _on_mini_prev(self) -> None:
        yr = self._mini_month.year
        mo = self._mini_month.month - 1
        if mo < 1:
            mo, yr = 12, yr - 1
        self._mini_month.set_month(yr, mo)
        self._update_mini_label()

    def _on_mini_next(self) -> None:
        yr = self._mini_month.year
        mo = self._mini_month.month + 1
        if mo > 12:
            mo, yr = 1, yr + 1
        self._mini_month.set_month(yr, mo)
        self._update_mini_label()

    def set_active_range(self, start: date, end: date) -> None:
        """Mirror the main view's date range in the mini-month."""
        self._mini_month.set_active_range(start, end)
        self._update_mini_label()

    def clear_active_range(self) -> None:
        self._mini_month.clear_active_range()
        self._update_mini_label()

    # ── Calendar list ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        for w in self._account_widgets:
            w.setParent(None)
            w.deleteLater()
        self._account_widgets.clear()
        self._checkboxes.clear()

        # Stretch item is always last; insert account groups before it.
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

        def _on(sig, aid=account_id):
            return lambda _checked=False: sig.emit(aid)

        act_rename = QAction("Rename…", menu)
        act_rename.triggered.connect(_on(self.rename_account_requested))
        menu.addAction(act_rename)

        act_reauth = QAction("Re-authenticate…", menu)
        act_reauth.triggered.connect(_on(self.reauth_account_requested))
        menu.addAction(act_reauth)

        act_sync = QAction("Sync now", menu)
        act_sync.triggered.connect(_on(self.sync_now_requested))
        menu.addAction(act_sync)

        menu.addSeparator()

        act_delete = QAction("Delete account…", menu)
        act_delete.triggered.connect(_on(self.delete_account_requested))
        menu.addAction(act_delete)

        menu_btn.setMenu(menu)
        h.addWidget(menu_btn)
        v.addWidget(header)

        cals = self._store.list_calendars(account.id, visible_only=False)
        for cal in cals:
            row = QWidget()
            row_h = QHBoxLayout(row)
            row_h.setContentsMargins(12, 0, 0, 0)
            row_h.setSpacing(6)

            swatch = _CalendarSwatch(cal.id, cal.color or "#5e9fff", self._store)
            swatch.color_changed.connect(self._on_calendar_color_changed)
            row_h.addWidget(swatch)

            cb = QCheckBox(cal.display_name)
            cb.setObjectName("cal-cb")
            cb.setChecked(bool(cal.is_visible))
            cb.toggled.connect(
                partial(
                    lambda cid, checked: self.calendar_visibility_changed.emit(
                        cid, bool(checked)
                    ),
                    cal.id,
                )
            )
            row_h.addWidget(cb, 1)
            v.addWidget(row)
            self._checkboxes[cal.id] = cb

        if not cals:
            placeholder = QLabel("(no calendars yet)")
            placeholder.setStyleSheet("color: #888; padding-left: 12px;")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignLeft)
            v.addWidget(placeholder)

        return container

    def _on_calendar_color_changed(self, calendar_id: str, new_hex: str) -> None:
        """Re-emit upward so MainWindow can refresh views with the new tint."""
        self.calendar_color_changed.emit(calendar_id, new_hex)
