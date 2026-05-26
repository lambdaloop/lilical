from __future__ import annotations

from datetime import date

from PySide6.QtCore import QByteArray, QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDrag, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
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
    """Colored chip showing calendar visibility; click to toggle, drag to reorder."""

    visibility_changed = Signal(str, bool)  # calendar_id, is_visible

    def __init__(
        self,
        calendar_id: str,
        color_hex: str,
        is_visible: bool,
        store: EventStore,
        account_id: str,
    ) -> None:
        super().__init__()
        self._calendar_id = calendar_id
        self._account_id = account_id
        self._color = color_hex
        self._visible = bool(is_visible)
        self._store = store
        self._drag_start_pos: QPoint | None = None
        self.setFixedSize(16, 16)
        self.setToolTip("Click to hide/show · Right-click for options")
        self.setAutoRaise(True)
        self._apply_style()
        self.clicked.connect(self._on_clicked)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if (
            self._drag_start_pos is not None
            and (event.pos() - self._drag_start_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._drag_start_pos = None
            self._start_drag(event)
            return
        super().mouseMoveEvent(event)

    def _start_drag(self, event) -> None:  # noqa: ANN001
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(
            "application/x-lilical-calendar-drag",
            QByteArray(self._calendar_id.encode()),
        )
        mime.setData(
            "application/x-lilical-account-id", QByteArray(self._account_id.encode())
        )
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(event.pos())
        drag.exec(Qt.DropAction.MoveAction)

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

    def update_color(self, new_hex: str) -> None:
        self._color = new_hex
        self._apply_style()


class _CalendarRow(QWidget):
    """Calendar row; left-click anywhere delegates to the chip to toggle visibility."""

    def __init__(self, chip: _CalendarChip) -> None:
        super().__init__()
        self._chip = chip
        self.setObjectName("cal-row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._chip.click()
            event.accept()
            return
        super().mousePressEvent(event)


class _AccountHeader(QWidget):
    """Account header that doubles as a drag handle for reordering."""

    def __init__(self, account_id: str) -> None:
        super().__init__()
        self._account_id = account_id
        self._drag_start_pos: QPoint | None = None
        self.setObjectName("account-header")

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if (
            self._drag_start_pos is not None
            and (event.pos() - self._drag_start_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._drag_start_pos = None
            self._start_drag(event)
            return
        super().mouseMoveEvent(event)

    def _start_drag(self, event) -> None:  # noqa: ANN001
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(
            "application/x-lilical-account-drag", QByteArray(self._account_id.encode())
        )
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(event.pos())
        drag.exec(Qt.DropAction.MoveAction)


class _DropIndicator(QWidget):
    """Thin blue line shown at the insertion point during drag."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setStyleSheet("background: #3b82f6; border-radius: 1px;")
        self.hide()

    def place_at(self, y: int, indent: int = 0) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        w = parent.width() - indent - 8
        self.setGeometry(indent, y, max(w, 0), 3)
        self.show()


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

    def setText(self, text: str) -> None:  # noqa: N802, type: ignore[override]
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.width()
        )
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
    new_calendar_requested = Signal(str)  # account_id
    sync_now_requested = Signal(str)
    delete_account_requested = Signal(str)
    calendar_visibility_changed = Signal(str, bool)
    calendar_color_changed = Signal(str, str)  # calendar_id, new_hex
    rename_calendar_requested = Signal(str)  # calendar_id
    change_color_requested = Signal(str)  # calendar_id
    delete_calendar_requested = Signal(str)  # calendar_id
    refresh_calendar_requested = Signal(str)  # calendar_id (read-only subscriptions)
    unsubscribe_requested = Signal(str)  # calendar_id (read-only subscriptions)
    account_order_changed = Signal()
    calendar_order_changed = Signal(str)  # account_id
    date_selected = Signal(date)  # from mini-month

    def __init__(
        self,
        store: EventStore,
        add_account_callback=None,
        cal_info_provider=None,
        account_meta_provider=None,
        subscribe_callback=None,
    ) -> None:
        super().__init__()
        self._store = store
        self._add_account_callback = add_account_callback
        self._subscribe_callback = subscribe_callback
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._account_meta_provider = account_meta_provider or (lambda: {})
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
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_widget = QWidget()
        self._cal_layout = QVBoxLayout(self._scroll_widget)
        self._cal_layout.setContentsMargins(0, 0, 0, 0)
        self._cal_layout.setSpacing(2)
        self._cal_layout.addStretch(1)
        self._scroll_area.setWidget(self._scroll_widget)
        layout.addWidget(self._scroll_area, 1)

        add_btn = QPushButton("+ Add account")
        add_btn.setObjectName("add-account")
        if add_account_callback is not None:
            add_btn.clicked.connect(add_account_callback)
        layout.addWidget(add_btn)

        sub_btn = QPushButton("+ Subscribe to calendar…")
        sub_btn.setObjectName("add-account")
        if subscribe_callback is not None:
            sub_btn.clicked.connect(subscribe_callback)
        layout.addWidget(sub_btn)

        self._chips: dict[str, _CalendarChip] = {}
        self._account_widgets: list[QWidget] = []
        self._account_widget_map: dict[str, QWidget] = {}  # account_id → group widget
        self._cal_snapshot: list[tuple] = []
        self._drag_active = False
        self._drag_kind: str | None = None
        self._drag_info: dict = {}
        self._drop_insert_idx = 0
        self._scroll_timer: QTimer | None = None
        self._drop_indicator = _DropIndicator(self._scroll_widget)
        self.setAcceptDrops(True)
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

    def set_week_start(self, week_start: str) -> None:
        self._mini_month.set_week_start(week_start)

    # ── Calendar list ──────────────────────────────────────────────────────

    def _build_snapshot(self) -> list[tuple]:
        cal_info = self._cal_info_provider()
        account_meta = self._account_meta_provider()
        cals_by_account: dict[str, list] = {}
        for ci in cal_info.values():
            cals_by_account.setdefault(ci.account_id, []).append(ci)
        result = []
        for acc_id, acc_meta in account_meta.items():
            display_name = acc_meta[0]
            cals = cals_by_account.get(acc_id, [])
            result.append(
                (
                    acc_id,
                    display_name,
                    tuple(
                        (ci.id, ci.display_name, ci.color, ci.visible) for ci in cals
                    ),
                )
            )
        return result

    def refresh(self) -> None:
        if self._drag_active:
            return
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

        account_meta = self._account_meta_provider()
        cal_info = self._cal_info_provider()
        for acc_id, acc_meta in account_meta.items():
            cals = [ci for ci in cal_info.values() if ci.account_id == acc_id]
            group = self._build_account_group_from_data(acc_id, acc_meta, cals)
            self._cal_layout.insertWidget(insert_at, group)
            insert_at += 1
            self._account_widgets.append(group)
            self._account_widget_map[acc_id] = group

    def refresh_for_account(self, account_id: str) -> None:
        """Rebuild only one account's calendar group; skip if nothing changed."""
        if self._drag_active:
            return
        new_snapshot = self._build_snapshot()
        if new_snapshot == self._cal_snapshot:
            return
        # Don't update _cal_snapshot yet: if we need to fall back to refresh(),
        # it must see the pending change and set the snapshot itself.

        account_meta = self._account_meta_provider()
        acc_meta = account_meta.get(account_id)
        old_widget = self._account_widget_map.get(account_id)
        if acc_meta is None or old_widget is None:
            self.refresh()
            return

        self._cal_snapshot = new_snapshot
        # Remove stale chips for this account.
        for cid in list(self._chips):
            if self._chips[cid].parent() is old_widget or _is_inside(
                self._chips[cid], old_widget
            ):
                del self._chips[cid]

        insert_at = self._cal_layout.indexOf(old_widget)
        old_widget.setParent(None)  # type: ignore[call-arg]
        old_widget.deleteLater()
        self._account_widgets.remove(old_widget)

        cal_info = self._cal_info_provider()
        cals = [ci for ci in cal_info.values() if ci.account_id == account_id]
        new_group = self._build_account_group_from_data(account_id, acc_meta, cals)
        self._cal_layout.insertWidget(insert_at, new_group)
        self._account_widgets.insert(insert_at, new_group)
        self._account_widget_map[account_id] = new_group

    def _build_account_group_from_data(
        self, acc_id: str, acc_meta: tuple, cals: list
    ) -> QWidget:
        """Build the account group widget from pre-fetched data.

        acc_meta is a (display_name, identity, kind) tuple.
        cals is a list of CalInfo objects for this account.
        """
        display_name, identity, kind = acc_meta
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 10, 0, 6)
        v.setSpacing(2)

        header = _AccountHeader(account_id=acc_id)
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        label = _ElidedLabel(display_name.upper())
        label.setObjectName("account-heading")
        label.setToolTip(f"{identity} ({kind})")
        h.addWidget(label, 1)

        menu_btn = QToolButton()
        menu_btn.setObjectName("account-menu-btn")
        menu_btn.setText("⋮")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setFixedSize(24, 22)
        menu_btn.setToolTip("Account actions")
        menu = QMenu(menu_btn)

        account_id = acc_id

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

        act_new_cal = QAction("New calendar…", menu)
        act_new_cal.triggered.connect(_on(self.new_calendar_requested))
        menu.addAction(act_new_cal)

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

        for ci in cals:
            chip = _CalendarChip(
                ci.id, ci.color or "#5e9fff", ci.visible, self._store, account_id=acc_id
            )
            chip.visibility_changed.connect(
                lambda cid, vis: self.calendar_visibility_changed.emit(cid, vis)
            )

            row = _CalendarRow(chip)
            row_h = QHBoxLayout(row)
            row_h.setContentsMargins(14, 2, 4, 2)
            row_h.setSpacing(8)
            row_h.addWidget(chip)

            name_label = _ElidedLabel(ci.display_name)
            name_label.setObjectName("cal-name")
            row_h.addWidget(name_label, 1)

            calendar_id = ci.id
            read_only = bool(getattr(ci, "read_only", False))

            def _make_row_menu(cid=calendar_id, ro=read_only):
                def _show_menu(pos) -> None:
                    menu = QMenu()
                    if ro:
                        act_refresh = QAction("Refresh now", menu)
                        act_refresh.triggered.connect(
                            lambda: self.refresh_calendar_requested.emit(cid)
                        )
                        menu.addAction(act_refresh)
                        act_color = QAction("Change color…", menu)
                        act_color.triggered.connect(
                            lambda: self.change_color_requested.emit(cid)
                        )
                        menu.addAction(act_color)
                        menu.addSeparator()
                        act_unsub = QAction("Unsubscribe…", menu)
                        act_unsub.triggered.connect(
                            lambda: self.unsubscribe_requested.emit(cid)
                        )
                        menu.addAction(act_unsub)
                    else:
                        act_rename = QAction("Rename…", menu)
                        act_rename.triggered.connect(
                            lambda: self.rename_calendar_requested.emit(cid)
                        )
                        menu.addAction(act_rename)
                        act_color = QAction("Change color…", menu)
                        act_color.triggered.connect(
                            lambda: self.change_color_requested.emit(cid)
                        )
                        menu.addAction(act_color)
                        menu.addSeparator()
                        act_delete = QAction("Delete calendar…", menu)
                        act_delete.triggered.connect(
                            lambda: self.delete_calendar_requested.emit(cid)
                        )
                        menu.addAction(act_delete)
                    menu.exec(QCursor.pos())
                return _show_menu

            row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            row.customContextMenuRequested.connect(_make_row_menu())

            v.addWidget(row)
            self._chips[ci.id] = chip

        if not cals:
            placeholder = QLabel("(no calendars yet)")
            placeholder.setObjectName("cal-placeholder")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignLeft)
            v.addWidget(placeholder)

        return container

    # ── Drag-and-drop reordering ───────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001, N802
        mime = event.mimeData()
        if mime.hasFormat("application/x-lilical-account-drag"):
            self._drag_kind = "account"
            self._drag_info = {
                "source_id": bytes(
                    mime.data("application/x-lilical-account-drag")
                ).decode()
            }
        elif mime.hasFormat("application/x-lilical-calendar-drag"):
            self._drag_kind = "calendar"
            self._drag_info = {
                "source_id": bytes(
                    mime.data("application/x-lilical-calendar-drag")
                ).decode(),
                "source_account_id": bytes(
                    mime.data("application/x-lilical-account-id")
                ).decode(),
            }
        else:
            event.ignore()
            return

        self._drag_active = True
        if self._scroll_timer is not None:
            self._scroll_timer.stop()
        timer = QTimer(self)
        timer.timeout.connect(self._on_auto_scroll)
        timer.start(30)
        self._scroll_timer = timer
        event.accept()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._update_drop_indicator(event)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hide_drop_indicator()
        self._stop_auto_scroll()
        self._drag_active = False
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hide_drop_indicator()
        self._stop_auto_scroll()
        self._drag_active = False
        self._handle_drop(event)
        event.accept()

    def _hide_drop_indicator(self) -> None:
        self._drop_indicator.hide()

    def _stop_auto_scroll(self) -> None:
        if self._scroll_timer is not None:
            self._scroll_timer.stop()
            self._scroll_timer = None

    def _on_auto_scroll(self) -> None:
        viewport = self._scroll_area.viewport()
        pos = viewport.mapFromGlobal(QCursor.pos())
        margin = 40
        bar = self._scroll_area.verticalScrollBar()
        scrolled = False
        if pos.y() < margin:
            bar.setValue(max(bar.value() - 5, bar.minimum()))
            scrolled = True
        elif pos.y() > viewport.height() - margin:
            bar.setValue(min(bar.value() + 5, bar.maximum()))
            scrolled = True
        if scrolled:
            pos_scroll = self._scroll_widget.mapFromGlobal(QCursor.pos())
            self._update_drop_indicator_at(pos_scroll)

    def _update_drop_indicator(self, event) -> None:  # noqa: ANN001
        pos = self._scroll_widget.mapFrom(self, event.pos())
        self._update_drop_indicator_at(pos)

    def _update_drop_indicator_at(self, pos: QPoint) -> None:
        y = pos.y()

        if self._drag_kind == "account":
            insert_idx = self._find_account_insert_index(y)
            self._drop_insert_idx = insert_idx
            if insert_idx < 0 or insert_idx > len(self._account_widgets):
                self._drop_indicator.hide()
                return
            indicator_y = (
                self._account_widgets[insert_idx].geometry().top()
                if insert_idx < len(self._account_widgets)
                else self._account_widgets[-1].geometry().bottom()
            )
            self._drop_indicator.place_at(y=indicator_y, indent=0)

        elif self._drag_kind == "calendar":
            source_account_id = self._drag_info["source_account_id"]
            group = self._account_widget_map.get(source_account_id)
            if group is None:
                self._drop_indicator.hide()
                return
            group_top = group.mapTo(self._scroll_widget, QPoint(0, 0)).y()
            group_bot = group_top + group.geometry().height()
            if not (group_top <= y <= group_bot):
                self._drop_indicator.hide()
                return

            rows = self._get_calendar_rows(group)
            insert_idx = self._find_calendar_insert_index(y, group_top, rows)
            self._drop_insert_idx = insert_idx
            if insert_idx < 0:
                self._drop_indicator.hide()
                return
            if insert_idx < len(rows):
                row_top = rows[insert_idx].mapTo(self._scroll_widget, QPoint(0, 0)).y()
                indicator_y = row_top
            elif rows:
                row_bot = (
                    rows[-1].mapTo(self._scroll_widget, QPoint(0, 0)).y()
                    + rows[-1].height()
                )
                indicator_y = row_bot
            else:
                layout = group.layout()
                header = None
                if layout is not None and layout.count() > 0:
                    item = layout.itemAt(0)
                    header = item.widget() if item is not None else None
                indicator_y = group_top + (header.height() if header else 0)
            self._drop_indicator.place_at(y=indicator_y, indent=14)

        else:
            self._drop_indicator.hide()

    def _find_account_insert_index(self, y: int) -> int:
        for i, w in enumerate(self._account_widgets):
            top = w.geometry().top()
            mid = top + w.geometry().height() // 2
            if y < mid:
                return i
        return len(self._account_widgets)

    def _find_calendar_insert_index(
        self, y: int, group_top: int, rows: list[QWidget]
    ) -> int:
        for i, row in enumerate(rows):
            row_top = row.mapTo(self._scroll_widget, QPoint(0, 0)).y()
            mid = row_top + row.height() // 2
            if y < mid:
                return i
        return len(rows)

    @staticmethod
    def _get_calendar_rows(group: QWidget) -> list[QWidget]:
        rows = []
        layout = group.layout()
        if layout is None:
            return rows
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None and w.objectName() == "cal-row":
                rows.append(w)
        return rows

    def _handle_drop(self, event) -> None:  # noqa: ANN001
        if self._drag_kind == "account":
            self._handle_account_drop()
        elif self._drag_kind == "calendar":
            self._handle_calendar_drop()

    def _handle_account_drop(self) -> None:
        source_id = self._drag_info["source_id"]
        insert_idx = self._drop_insert_idx

        current_ids = list(self._account_meta_provider().keys())
        try:
            source_idx = current_ids.index(source_id)
        except ValueError:
            return
        current_ids.pop(source_idx)
        if insert_idx > source_idx:
            insert_idx -= 1
        if source_idx == insert_idx:
            return
        current_ids.insert(insert_idx, source_id)
        orders = [(aid, i) for i, aid in enumerate(current_ids)]
        self._store.set_account_orders(orders)
        self.account_order_changed.emit()

    def _handle_calendar_drop(self) -> None:
        source_id = self._drag_info["source_id"]
        source_account_id = self._drag_info["source_account_id"]
        insert_idx = self._drop_insert_idx

        cal_info = self._cal_info_provider()
        current_ids = [
            ci.id for ci in cal_info.values() if ci.account_id == source_account_id
        ]
        try:
            source_idx = current_ids.index(source_id)
        except ValueError:
            return
        current_ids.pop(source_idx)
        if insert_idx > source_idx:
            insert_idx -= 1
        if source_idx == insert_idx:
            return
        current_ids.insert(insert_idx, source_id)
        orders = [(cid, i) for i, cid in enumerate(current_ids)]
        self._store.set_calendar_orders(orders)
        self.calendar_order_changed.emit(source_account_id)
