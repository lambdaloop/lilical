from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore
from lilical.ui.widgets.mini_month import MiniMonthGrid


class _CalendarChip(QToolButton):
    """Colored chip showing calendar visibility; click to toggle, right-click for color."""

    visibility_changed = Signal(str, bool)  # calendar_id, is_visible
    color_changed = Signal(str, str)  # calendar_id, new_hex

    def __init__(
        self, calendar_id: str, color_hex: str, is_visible: bool, store: EventStore
    ) -> None:
        super().__init__()
        self._calendar_id = calendar_id
        self._color = color_hex
        self._visible = bool(is_visible)
        self._store = store
        self.setFixedSize(16, 16)
        self.setToolTip("Click to hide/show · Right-click to change color")
        self.setAutoRaise(True)
        self._apply_style()
        self.clicked.connect(self._on_clicked)

    def _apply_style(self) -> None:
        border_color = QColor(self._color).darker(130).name()
        if self._visible:
            self.setStyleSheet(
                f"QToolButton {{"
                f"  background-color: {self._color};"
                f"  border: 2px solid {border_color};"
                f"  border-radius: 4px;"
                f"}}"
                f"QToolButton:hover {{ border-color: #ffffff; }}"
            )
        else:
            self.setStyleSheet(
                f"QToolButton {{"
                f"  background-color: transparent;"
                f"  border: 2px solid {self._color};"
                f"  border-radius: 4px;"
                f"}}"
                f"QToolButton:hover {{ border-color: #ffffff; }}"
            )

    def _on_clicked(self) -> None:
        self._visible = not self._visible
        self._apply_style()
        self.visibility_changed.emit(self._calendar_id, self._visible)

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001
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
        self._store.set_calendar_color(self._calendar_id, new_hex)
        self._apply_style()
        self.color_changed.emit(self._calendar_id, new_hex)


class _ElidedLabel(QLabel):
    """QLabel that elides text with '…' when narrower than its content."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setToolTip(text)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        super().setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(
            self.rect(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )


def _is_inside(widget: QWidget, ancestor: QWidget) -> bool:
    p = widget.parent()
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent()
    return False


class Sidebar(QWidget):
    rename_account_requested = Signal(str)
    reauth_account_requested = Signal(str)
    choose_calendars_requested = Signal(str)  # account_id
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
        self.setObjectName("sidebar")
        self.setMinimumWidth(180)
        self.setMaximumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(8)

        # ── Mini-month picker ──────────────────────────────────────────────
        mini_header = QHBoxLayout()
        self._mini_prev = QToolButton()
        self._mini_prev.setText("‹")
        self._mini_prev.setObjectName("mini-nav")
        self._mini_prev.setAutoRaise(True)
        self._mini_label = QLabel()
        self._mini_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mini_label.setObjectName("mini-month-label")
        self._mini_next = QToolButton()
        self._mini_next.setText("›")
        self._mini_next.setObjectName("mini-nav")
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

        # ── Calendars label ────────────────────────────────────────────────
        cal_title = QLabel("CALENDARS")
        cal_title.setObjectName("section-heading")
        layout.addWidget(cal_title)

        # ── Calendar list (scrollable) ─────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_widget = QWidget()
        self._cal_layout = QVBoxLayout(self._scroll_widget)
        self._cal_layout.setContentsMargins(0, 0, 0, 0)
        self._cal_layout.setSpacing(2)
        self._cal_layout.addStretch(1)
        scroll.setWidget(self._scroll_widget)
        layout.addWidget(scroll, 1)

        add_btn = QPushButton("+ Add account")
        add_btn.setObjectName("add-account")
        if add_account_callback is not None:
            add_btn.clicked.connect(add_account_callback)
        layout.addWidget(add_btn)

        self._chips: dict[str, _CalendarChip] = {}
        self._account_widgets: list[QWidget] = []
        self._account_widget_map: dict[str, QWidget] = {}  # account_id → group widget
        self._cal_snapshot: list[tuple] = []
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

    def _build_snapshot(self) -> list[tuple]:
        return [
            (
                acc.id,
                acc.display_name,
                tuple(
                    (c.id, c.display_name, c.color, c.is_visible)
                    for c in self._store.list_calendars(acc.id, visible_only=True)
                ),
            )
            for acc in self._store.list_accounts()
        ]

    def refresh(self) -> None:
        new_snapshot = self._build_snapshot()
        if new_snapshot == self._cal_snapshot:
            return
        self._cal_snapshot = new_snapshot

        for w in self._account_widgets:
            w.setParent(None)
            w.deleteLater()
        self._account_widgets.clear()
        self._account_widget_map.clear()
        self._chips.clear()

        # Stretch item is always last; insert account groups before it.
        insert_at = max(self._cal_layout.count() - 1, 0)

        for acc in self._store.list_accounts():
            group = self._build_account_group(acc)
            self._cal_layout.insertWidget(insert_at, group)
            insert_at += 1
            self._account_widgets.append(group)
            self._account_widget_map[acc.id] = group

    def refresh_for_account(self, account_id: str) -> None:
        """Rebuild only one account's calendar group; skip if nothing changed."""
        new_snapshot = self._build_snapshot()
        if new_snapshot == self._cal_snapshot:
            return
        self._cal_snapshot = new_snapshot

        acc = self._store.get_account(account_id)
        old_widget = self._account_widget_map.get(account_id)
        if acc is None or old_widget is None:
            self.refresh()
            return

        # Remove stale chips for this account.
        for cid in list(self._chips):
            if self._chips[cid].parent() is old_widget or _is_inside(self._chips[cid], old_widget):
                del self._chips[cid]

        insert_at = self._cal_layout.indexOf(old_widget)
        old_widget.setParent(None)  # type: ignore[call-arg]
        old_widget.deleteLater()
        self._account_widgets.remove(old_widget)

        new_group = self._build_account_group(acc)
        self._cal_layout.insertWidget(insert_at, new_group)
        self._account_widgets.insert(insert_at, new_group)
        self._account_widget_map[account_id] = new_group

    def _build_account_group(self, account) -> QWidget:
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 10, 0, 6)
        v.setSpacing(2)

        header = QWidget()
        header.setObjectName("account-header")
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        label = _ElidedLabel(account.display_name.upper())
        label.setObjectName("account-heading")
        label.setToolTip(f"{account.identity} ({account.kind})")
        h.addWidget(label, 1)

        menu_btn = QToolButton()
        menu_btn.setObjectName("account-menu-btn")
        menu_btn.setText("⋮")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setFixedSize(24, 22)
        menu_btn.setToolTip("Account actions")
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

        act_calendars = QAction("Choose calendars…", menu)
        act_calendars.triggered.connect(_on(self.choose_calendars_requested))
        menu.addAction(act_calendars)

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
            row.setObjectName("cal-row")
            row_h = QHBoxLayout(row)
            row_h.setContentsMargins(14, 2, 4, 2)
            row_h.setSpacing(8)

            chip = _CalendarChip(cal.id, cal.color or "#5e9fff", cal.is_visible, self._store)
            chip.visibility_changed.connect(
                lambda cid, vis: self.calendar_visibility_changed.emit(cid, vis)
            )
            chip.color_changed.connect(self._on_calendar_color_changed)
            row_h.addWidget(chip)

            name_label = _ElidedLabel(cal.display_name)
            name_label.setObjectName("cal-name")
            row_h.addWidget(name_label, 1)
            v.addWidget(row)
            self._chips[cal.id] = chip

        if not cals:
            placeholder = QLabel("(no calendars yet)")
            placeholder.setObjectName("cal-placeholder")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignLeft)
            v.addWidget(placeholder)

        return container

    def _on_calendar_color_changed(self, calendar_id: str, new_hex: str) -> None:
        """Re-emit upward so MainWindow can refresh views with the new tint."""
        self.calendar_color_changed.emit(calendar_id, new_hex)
