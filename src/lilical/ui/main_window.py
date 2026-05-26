from __future__ import annotations

import asyncio
import calendar
import dataclasses
import logging
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import override

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme as _theme_module
from lilical.ui.sidebar import Sidebar
from lilical.ui.tray import SystemTray
from lilical.ui.views.agenda import AgendaView
from lilical.ui.views.day import DayView
from lilical.ui.views.month import MonthView
from lilical.ui.views.week import VALID_DAY_COUNTS, WeekView
from lilical.ui.widgets.account_setup import AccountSetupDialog
from lilical.ui.widgets.event_chip import ChipMode
from lilical.ui.widgets.inspector_pane import InspectorPane

log = logging.getLogger(__name__)

_VIEW_NAMES = ["Month", "Week", "Day", "Agenda"]
_DEFAULT_VIEW = "Week"


def _is_read_only_cal(cal) -> bool:  # noqa: ANN001
    """A calendar is read-only when its access_role is reader-tier.

    Currently catches both subscription calendars (access_role='reader',
    set when the subscription is created) and any Google/Graph calendar
    the user only has read access to.
    """
    role = (getattr(cal, "access_role", "") or "").lower()
    return role in ("reader", "freebusyreader")


@dataclasses.dataclass(frozen=True)
class CalInfo:
    """Immutable snapshot of a calendar's metadata for use in view builders."""

    id: str
    display_name: str
    color: str | None
    account_id: str
    visible: bool
    read_only: bool = False


class _ElidingLabel(QLabel):
    """QLabel pinned to a fixed width that auto-ellipsizes long text.

    Used for the sync status pill: a long error message must not push the
    status bar (and therefore the whole window) wider. The full text is
    preserved in the tooltip.
    """

    def __init__(
        self, text: str = "", *, width: int = 260, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setFixedWidth(width)
        self.setToolTip(text)
        # Don't try to wrap — we always render a single elided line.
        self.setWordWrap(False)

    @override
    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self.update()

    @override
    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        fm = QFontMetrics(self.font())
        # Reserve a few pixels of padding either side so the text doesn't
        # touch the bordering widgets.
        elided = fm.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.width() - 4
        )
        painter.drawText(
            self.rect().adjusted(2, 0, -2, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )


class _SyncStatusWidget(QWidget):
    """Compact coloured pill showing sync state in the status bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self._dot = QLabel("●")
        # `_ElidingLabel` is pinned to a fixed width and auto-ellipsizes; this
        # prevents a long sync-error string from widening the status bar and
        # thus the whole window. Full text remains in the tooltip.
        self._text = _ElidingLabel("Ready", width=260)
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
                f"🔑 {label}: Authentication failed — "
                f"right-click account to re-authenticate"
            )
            self._text.setToolTip("")

    def set_ready(self) -> None:
        self._dot.setStyleSheet("color: #6ee896;")
        self._text.setText("Ready")


class MainWindow(QMainWindow):
    def __init__(
        self, *, config, event_store, sync_engine, recurrence, secrets, backend_factory
    ) -> None:
        super().__init__()
        self._cfg = config
        self._store = event_store
        self._sync = sync_engine
        self._secrets = secrets
        self._backend_factory = backend_factory
        self._current_view: QWidget | None = None
        self._view_actions: dict[str, QAction] = {}
        # Account display names for sync-status labels (legacy; kept alongside _account_meta).  # noqa: E501
        self._account_display_names: dict[str, str] = {}
        # Per-account metadata for sidebar tooltips: id → (display_name, identity, kind).  # noqa: E501
        self._account_meta: dict[str, tuple[str, str, str]] = {}
        # Per-calendar metadata snapshot; rebuilt off-thread after sync ticks.
        self._cal_info: dict[str, CalInfo] = {}
        for acc in self._store.list_accounts():
            self._account_display_names[acc.id] = acc.display_name
            self._account_meta[acc.id] = (acc.display_name, acc.identity, acc.kind)
            for cal in self._store.list_calendars(acc.id, included_only=True):
                self._cal_info[cal.id] = CalInfo(
                    id=cal.id,
                    display_name=cal.display_name,
                    color=cal.color,
                    account_id=acc.id,
                    visible=bool(cal.is_visible),
                    read_only=_is_read_only_cal(cal),
                )
        self._theme_qss_cache: dict[str, str] = {}

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
        week_start_raw = str(self._settings.value("week_start", "monday") or "monday")
        self._week_start: str = (
            week_start_raw
            if week_start_raw in ("monday", "sunday", "saturday")
            else "monday"
        )
        _raw_scale = float(self._settings.value("ui_scale", 1.0) or 1.0)  # type: ignore[reportArgumentType]
        self._ui_scale: float = (
            _raw_scale if _raw_scale in _theme_module.UI_SCALE_PRESETS else 1.0
        )

        self.setWindowTitle("lilical")
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # ── Sidebar ────────────────────────────────────────────────────────
        self._sidebar = Sidebar(
            event_store,
            add_account_callback=self._add_account,
            cal_info_provider=self._cal_info_provider,
            account_meta_provider=self._account_meta_provider,
            subscribe_callback=self._subscribe,
        )
        self._sidebar.rename_account_requested.connect(self._on_rename_account)
        self._sidebar.reauth_account_requested.connect(self._on_reauth_account)
        self._sidebar.choose_calendars_requested.connect(self._on_choose_calendars)
        self._sidebar.sync_now_requested.connect(self._on_sync_now_account)
        self._sidebar.delete_account_requested.connect(self._on_delete_account)
        self._sidebar.calendar_visibility_changed.connect(
            self._on_calendar_visibility_changed
        )
        self._sidebar.calendar_color_changed.connect(self._on_calendar_color_changed)
        self._sidebar.rename_calendar_requested.connect(self._on_rename_calendar)
        self._sidebar.change_color_requested.connect(self._on_change_calendar_color)
        self._sidebar.new_calendar_requested.connect(self._on_new_calendar)
        self._sidebar.delete_calendar_requested.connect(self._on_delete_calendar)
        self._sidebar.refresh_calendar_requested.connect(self._on_refresh_calendar)
        self._sidebar.unsubscribe_requested.connect(self._on_unsubscribe)
        self._sidebar.account_order_changed.connect(self._on_account_order_changed)
        self._sidebar.calendar_order_changed.connect(self._on_calendar_order_changed)
        self._sidebar.date_selected.connect(self._on_sidebar_date_selected)

        # Top toolbar via QMainWindow's standard API — embedding a QToolBar
        # inside a QVBoxLayout has caused it to render at zero height on some
        # platforms; addToolBar() puts it in the dedicated toolbar area where
        # Qt sizes it correctly.
        self._toolbar = self._build_toolbar()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)

        # ── Right side: stacked views ─────────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Stacked widget (manual, no QStackedWidget, for direct access)
        self._view_container = QWidget()
        self._view_stack_layout = QVBoxLayout(self._view_container)
        self._view_stack_layout.setContentsMargins(0, 0, 0, 0)

        saved_snap = int(self._settings.value("snap_minutes", 15) or 15)  # type: ignore[reportArgumentType]
        if saved_snap not in (5, 10, 15, 30, 60):
            saved_snap = 15
        self._snap_minutes: int = saved_snap
        # Views are constructed lazily on first switch; only the default view
        # is built at startup.
        self._views: dict[str, QWidget] = {}

        right_layout.addWidget(self._view_container, 1)

        # Right-side inspector pane — shows hovered event details + cluster
        # context. Constructed before views so it can be passed into them.
        self._inspector = InspectorPane(cal_info_provider=self._cal_info_provider)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(right)
        splitter.addWidget(self._inspector)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([240, 760, 200])
        splitter.setChildrenCollapsible(False)
        main_layout.addWidget(splitter)

        # ── Status bar ─────────────────────────────────────────────────────
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._sync_status = _SyncStatusWidget()
        self._statusbar.addPermanentWidget(self._sync_status)
        self._syncing_accounts: set[str] = set()

        # ── System tray ────────────────────────────────────────────────────
        self._tray = SystemTray(self)
        _app = QApplication.instance()
        if _app is not None and _app.property("__tray_available") is not False:
            self._tray.show()

        # ── Scale + Theme ─────────────────────────────────────────────────
        self._apply_ui_scale_globals(self._ui_scale)
        self._apply_theme(self._theme)

        # ── Signal wiring ──────────────────────────────────────────────────
        self._events_changed_pending: set[str] = set()
        self._events_changed_calendars: set[str] = set()
        self._local_events_pending = False
        self._events_changed_timer = QTimer(self)
        self._events_changed_timer.setSingleShot(True)
        self._events_changed_timer.setInterval(150)
        self._events_changed_timer.timeout.connect(self._flush_events_changed)

        self._store.events_changed.connect(self._on_events_changed)
        self._store.local_events_changed.connect(self._on_local_events_changed)
        self._store.cal_metadata_changed.connect(self._on_cal_metadata_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_progress.connect(self._on_sync_progress)
        self._sync.sync_finished.connect(self._on_sync_finished)
        self._sync.sync_failed.connect(self._on_sync_failed)
        self._sync.auth_expired.connect(self._on_auth_expired)

        # ── Keyboard shortcuts ─────────────────────────────────────────────
        self._setup_shortcuts()

        # ── Initial state ──────────────────────────────────────────────────
        self._sidebar.set_week_start(self._week_start)
        self._switch_view(self._default_view_name)

        # Rebuild instances asynchronously to avoid first-launch freeze
        self._fire_async(self._rebuild_instances_async(), "rebuild_instances")

    # ── Toolbar ────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QToolBar:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setMinimumWidth(0)
        # Compact toolbar: tighter padding, smaller icons, no text-under-icons.
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        tb.setContentsMargins(0, 0, 0, 0)
        # Tighten button + label padding here rather than in the qss so the
        # rules also apply to the light theme without duplication.
        tb.setStyleSheet(
            "QToolBar { spacing: 0px; padding: 0px; }"
            "QToolBar::separator { width: 1px; margin: 4px 3px; }"
            "QToolBar QToolButton { padding: 2px 6px; }"
            "QToolBar QLabel { padding: 0 4px; }"
        )

        # View switcher
        for name in _VIEW_NAMES:
            act = QAction(name, self)
            act.setCheckable(True)
            act.triggered.connect(lambda _, n=name: self._switch_view(n))
            tb.addAction(act)
            self._view_actions[name] = act

        tb.addSeparator()

        # ── Week view: day-count slider (1,2,3,4,5,7,10,14) ─────────────
        self._day_count_label = QLabel("Days:")
        tb.addWidget(self._day_count_label)

        self._day_count_slider = QSlider(Qt.Orientation.Horizontal)
        self._day_count_slider.setMinimum(0)
        self._day_count_slider.setMaximum(len(VALID_DAY_COUNTS) - 1)
        self._day_count_slider.setSingleStep(1)
        self._day_count_slider.setPageStep(1)
        self._day_count_slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self._day_count_slider.setFixedWidth(110)
        # Restore last-used value, default 7.
        saved_dc = int(self._settings.value("week_day_count", 7) or 7)  # type: ignore[reportArgumentType]
        if saved_dc not in VALID_DAY_COUNTS:
            saved_dc = 7
        self._day_count_slider.setValue(VALID_DAY_COUNTS.index(saved_dc))
        self._day_count_slider.valueChanged.connect(self._on_day_count_changed)
        tb.addWidget(self._day_count_slider)

        self._pending_day_count: int = saved_dc
        self._day_count_debounce = QTimer(self)
        self._day_count_debounce.setSingleShot(True)
        self._day_count_debounce.setInterval(80)
        self._day_count_debounce.timeout.connect(self._apply_pending_day_count)

        self._day_count_value_label = QLabel(str(saved_dc))
        # Fixed width so "1" vs "14" doesn't change the toolbar's min size.
        self._day_count_value_label.setFixedWidth(22)
        self._day_count_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self._day_count_value_label)

        tb.addSeparator()

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

        # Current range label (pushed to right by spacer)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tb.addWidget(spacer)

        self._range_label = QLabel()
        # Lock the width so the toolbar's minimum-size hint doesn't grow
        # (and push the window wider) when the range string lengthens —
        # e.g. switching day-count from 1 → 14 turns "May 11, 2026" into
        # "May 11 – May 24, 2026".
        _range_font = QFont(self._range_label.font())
        _range_font.setPointSize(16)
        _range_font.setBold(True)
        self._range_label.setFont(_range_font)
        self._range_label.setFixedWidth(310)
        self._range_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

        # Pin the settings button to the far right with its own expanding spacer
        # and a divider so it reads visually as a separate "top-right" control,
        # not just another action button.
        right_spacer = QWidget()
        right_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        tb.addWidget(right_spacer)
        tb.addSeparator()

        prefs_btn = QToolButton()
        prefs_btn.setText("⚙")
        prefs_btn.setToolTip("Settings  (Ctrl+,)")
        # Slightly larger glyph so the gear reads as the primary affordance,
        # but no extra padding — the toolbar-level rule already keeps it tight.
        prefs_btn.setStyleSheet("font-size: 16px;")
        prefs_btn.clicked.connect(self._open_preferences)
        tb.addWidget(prefs_btn)

        return tb

    def _update_range_label(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "range_label"):
            self._range_label.setText(view.range_label())  # type: ignore[reportAttributeAccessIssue]
        # Always keep the mini-month in lockstep with whatever the main view
        # is currently showing.
        self._sync_mini_month()

    def _sync_mini_month(self) -> None:
        """Mirror the current view's date range in the sidebar mini-month."""
        view = self._views.get(self._current_view_name)
        if view is None:
            return
        name = self._current_view_name
        try:
            if name == "Day":
                d = getattr(view, "_day", None)
                if isinstance(d, date):
                    self._sidebar.set_active_range(d, d)
            elif name == "Week":
                start = getattr(view, "_start", None)
                count = getattr(view, "_day_count", None)
                if isinstance(start, date) and isinstance(count, int) and count > 0:
                    self._sidebar.set_active_range(
                        start, start + timedelta(days=count - 1)
                    )
            elif name == "Month":
                y = getattr(view, "_year", None)
                m = getattr(view, "_month", None)
                if isinstance(y, int) and isinstance(m, int):
                    last = calendar.monthrange(y, m)[1]
                    self._sidebar.set_active_range(date(y, m, 1), date(y, m, last))
            elif name == "Agenda":
                start = getattr(view, "_start", None)
                if isinstance(start, date):
                    # Agenda is a list of upcoming events — highlight just
                    # the start date so the user can see where it anchors.
                    self._sidebar.set_active_range(start, start)
        except Exception:
            log.exception("Failed to sync mini-month")

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
        sc("Ctrl+Shift+R", self._deep_refresh_all)

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
            view.navigate(-1)  # type: ignore[reportAttributeAccessIssue]
            self._update_range_label()

    def _nav_next(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "navigate"):
            view.navigate(1)  # type: ignore[reportAttributeAccessIssue]
            self._update_range_label()

    def _nav_today(self) -> None:
        view = self._views.get(self._current_view_name)
        if view and hasattr(view, "go_today"):
            view.go_today()  # type: ignore[reportAttributeAccessIssue]
            self._update_range_label()

    def _on_sidebar_date_selected(self, d: date) -> None:
        """Navigate the current view to the selected date without switching views."""
        view = self._current_view
        if isinstance(view, DayView):
            view.set_day(d)
        elif isinstance(view, (WeekView, MonthView, AgendaView)):
            view.go_to_date(d)
        self._update_range_label()
        self._sync_mini_month()

    def _on_day_activated(self, d) -> None:
        """Switch to Day view and focus the given date."""
        self._switch_view("Day", refresh=False)
        day_view = self._views.get("Day")
        if isinstance(day_view, DayView):
            day_view.set_day(d)

    def _on_month_new_event_requested(self, d) -> None:
        """User double-clicked empty day cell in Month view: open new-event dialog."""
        from datetime import datetime

        dt_start = datetime(d.year, d.month, d.day, 9, 0, 0).astimezone()
        dt_end = datetime(d.year, d.month, d.day, 10, 0, 0).astimezone()
        self._new_event(default_dt=dt_start, default_dtend=dt_end)

    # ── View switching ─────────────────────────────────────────────────────

    def _construct_view(self, name: str) -> QWidget:
        saved_dc = int(self._settings.value("week_day_count", 7) or 7)  # type: ignore[reportArgumentType]
        if saved_dc not in VALID_DAY_COUNTS:
            saved_dc = 7
        saved_chip_mode = str(self._settings.value("chip_mode", "bars") or "bars")
        initial_mode = ChipMode.TEXT if saved_chip_mode == "text" else ChipMode.BARS
        saved_time_format = str(self._settings.value("time_format", "24h") or "24h")
        saved_snap = int(self._settings.value("snap_minutes", 15) or 15)  # type: ignore[reportArgumentType]
        if saved_snap not in (5, 10, 15, 30, 60):
            saved_snap = 15

        cip = self._cal_info_provider
        v: QWidget
        if name == "Month":
            mv = MonthView(self._store, cal_info_provider=cip)
            mv.day_activated.connect(self._on_day_activated)
            mv.new_event_requested.connect(self._on_month_new_event_requested)
            v = mv
        elif name == "Week":
            wv = WeekView(
                self._store,
                day_count=saved_dc,
                cal_info_provider=cip,
                inspector=self._inspector,
            )
            wv.day_header_activated.connect(self._on_day_activated)
            v = wv
        elif name == "Day":
            v = DayView(
                self._store, cal_info_provider=cip, inspector=self._inspector
            )
        elif name == "Agenda":
            v = AgendaView(self._store, cal_info_provider=cip)
        else:
            raise ValueError(f"Unknown view: {name}")

        saved_enable_completed = bool(
            int(self._settings.value("enable_completed_events", 0) or 0)  # type: ignore[reportArgumentType]
        )

        if hasattr(v, "set_chip_mode"):
            v.set_chip_mode(initial_mode)  # type: ignore[reportAttributeAccessIssue]
        if hasattr(v, "set_time_format"):
            v.set_time_format(saved_time_format)  # type: ignore[reportAttributeAccessIssue]
        if hasattr(v, "set_snap_minutes"):
            v.set_snap_minutes(saved_snap)  # type: ignore[reportAttributeAccessIssue]
        if hasattr(v, "set_week_start"):
            v.set_week_start(self._week_start)  # type: ignore[reportAttributeAccessIssue]
        if hasattr(v, "set_completed_events_enabled"):
            v.set_completed_events_enabled(saved_enable_completed)  # type: ignore[reportAttributeAccessIssue]

        self._view_stack_layout.addWidget(v)
        v.hide()
        return v

    def _switch_view(self, name: str, *, refresh: bool = True) -> None:
        if self._current_view is not None:
            self._current_view.hide()
        self._current_view_name = name
        view = self._views.get(name)
        just_constructed = view is None
        if view is None:
            view = self._construct_view(name)
            self._views[name] = view
        self._current_view = view
        view.show()
        # Brand-new view: resizeEvent fires after show() and schedules the
        # first refresh against the correct viewport size, so skip the
        # explicit call here. Returning view: refresh to pick up any changes
        # that occurred while it was hidden.
        if refresh and not just_constructed and hasattr(view, "refresh"):
            view.refresh()  # type: ignore[reportAttributeAccessIssue]
        # Update toolbar checkmarks
        for n, act in self._view_actions.items():
            act.setChecked(n == name)
        # Show day-count slider only in Week view.
        is_week = name == "Week"
        self._day_count_label.setVisible(is_week)
        self._day_count_slider.setVisible(is_week)
        self._day_count_value_label.setVisible(is_week)
        self._update_range_label()

    # ── Toolbar handlers ──────────────────────────────────────────────────

    def _on_day_count_changed(self, slider_value: int) -> None:
        if slider_value < 0 or slider_value >= len(VALID_DAY_COUNTS):
            return
        n = VALID_DAY_COUNTS[slider_value]
        self._day_count_value_label.setText(str(n))
        self._settings.setValue("week_day_count", n)
        self._pending_day_count = n
        self._day_count_debounce.start()

    def _apply_pending_day_count(self) -> None:
        week_view = self._views.get("Week")
        if isinstance(week_view, WeekView):
            week_view.set_day_count(self._pending_day_count)
            self._update_range_label()

    # ── Events ─────────────────────────────────────────────────────────────

    def _new_event(self, default_dt=None, default_dtend=None) -> None:
        from lilical.ui.widgets.event_dialog import EventDialog

        dlg = EventDialog(
            self, store=self._store, default_dt=default_dt, default_dtend=default_dtend
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:  # type: ignore[reportAttributeAccessIssue]
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

    def _deep_refresh_all(self) -> None:
        for acc in self._store.list_accounts():
            self._sync.force_full_resync(acc.id)
        self._sync_status.set_syncing("all accounts (full resync)")

    # ── Preferences ────────────────────────────────────────────────────────

    def _open_preferences(self) -> None:
        from lilical.ui.widgets.preferences_dialog import PreferencesDialog

        current_chip_mode = str(self._settings.value("chip_mode", "bars") or "bars")
        current_time_format = str(self._settings.value("time_format", "24h") or "24h")
        current_enable_completed = bool(
            int(self._settings.value("enable_completed_events", 0) or 0)  # type: ignore[reportArgumentType]
        )
        dlg = PreferencesDialog(
            self,
            current_theme=self._theme,
            current_default_view=self._default_view_name,
            current_snap_minutes=self._snap_minutes,
            current_chip_mode=current_chip_mode,
            current_time_format=current_time_format,
            current_week_start=self._week_start,
            current_enable_completed_events=current_enable_completed,
            current_ui_scale=self._ui_scale,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:  # type: ignore[reportAttributeAccessIssue]
            if dlg.theme != self._theme:
                self._theme = dlg.theme
                self._apply_theme(self._theme)
                self._settings.setValue("theme", self._theme)
            if (
                dlg.default_view != self._default_view_name
                and dlg.default_view in _VIEW_NAMES
            ):
                self._default_view_name = dlg.default_view
                self._settings.setValue("default_view", self._default_view_name)
            if dlg.snap_minutes != self._snap_minutes:
                self._snap_minutes = dlg.snap_minutes
                self._settings.setValue("snap_minutes", self._snap_minutes)
                for v in self._views.values():
                    if hasattr(v, "set_snap_minutes"):
                        v.set_snap_minutes(self._snap_minutes)  # type: ignore[reportAttributeAccessIssue]
            if dlg.chip_mode != current_chip_mode:
                self._settings.setValue("chip_mode", dlg.chip_mode)
                chip_mode_enum = (
                    ChipMode.TEXT if dlg.chip_mode == "text" else ChipMode.BARS
                )
                for v in self._views.values():
                    if hasattr(v, "set_chip_mode"):
                        v.set_chip_mode(chip_mode_enum)  # type: ignore[reportAttributeAccessIssue]
            if dlg.time_format != current_time_format:
                self._settings.setValue("time_format", dlg.time_format)
                for v in self._views.values():
                    if hasattr(v, "set_time_format"):
                        v.set_time_format(dlg.time_format)  # type: ignore[reportAttributeAccessIssue]
            if dlg.week_start != self._week_start and dlg.week_start in (
                "monday",
                "sunday",
                "saturday",
            ):
                self._week_start = dlg.week_start
                self._settings.setValue("week_start", self._week_start)
                for v in self._views.values():
                    if hasattr(v, "set_week_start"):
                        v.set_week_start(self._week_start)  # type: ignore[reportAttributeAccessIssue]
                self._sidebar.set_week_start(self._week_start)
            if dlg.enable_completed_events != current_enable_completed:
                self._settings.setValue(
                    "enable_completed_events", int(dlg.enable_completed_events)
                )
                for v in self._views.values():
                    if hasattr(v, "set_completed_events_enabled"):
                        v.set_completed_events_enabled(dlg.enable_completed_events)  # type: ignore[reportAttributeAccessIssue]
            if dlg.ui_scale != self._ui_scale:
                self._ui_scale = dlg.ui_scale
                self._settings.setValue("ui_scale", self._ui_scale)
                self._apply_ui_scale_globals(self._ui_scale)
                self._theme_qss_cache.clear()
                self._apply_theme(self._theme)
                # Discard all cached views — they hold layout constants baked at
                # construction time (scene rects, fixed heights, zoom limits).
                for v in list(self._views.values()):
                    self._view_stack_layout.removeWidget(v)
                    v.deleteLater()
                self._views.clear()
                self._current_view = None
                # Rebuild the mini-month so its setFixedHeight uses the new constants.
                self._sidebar._mini_month.reset_scale()  # type: ignore[reportPrivateUsage]
                self._switch_view(self._current_view_name)

    def _apply_ui_scale_globals(self, scale: float) -> None:
        _theme_module.apply_all_scales(scale)
        app = QApplication.instance()
        if app is not None:
            _pt = _theme_module.ui_base_font_pt()
            f = app.font()
            f.setPointSize(_pt)
            app.setFont(f)

    def _apply_theme(self, name: str) -> None:
        _theme_module.apply(name)
        if name not in self._theme_qss_cache:
            try:
                theme_path = Path(__file__).parent / "styles" / f"{name}.qss"
                if theme_path.exists():
                    with open(theme_path) as f:
                        qss = f.read()
                    # Resolve relative url(./…) to absolute so referenced assets
                    # (e.g. SVG checkmarks) load correctly regardless of cwd.
                    styles_dir = theme_path.parent.as_posix()
                    qss = qss.replace("url(./", f"url({styles_dir}/")
                    # Scale all font-size: Npt; values proportionally with UI scale.
                    scale = _theme_module.UI_SCALE

                    def _scale_pt(m: re.Match) -> str:  # type: ignore[type-arg]
                        pt = max(1, round(float(m.group(1)) * scale))
                        return f"font-size: {pt}pt;"

                    qss = re.sub(r"font-size:\s*(\d+(?:\.\d+)?)pt;", _scale_pt, qss)
                    self._theme_qss_cache[name] = qss
                else:
                    log.warning("Theme file not found: %s", theme_path)
                    self._theme_qss_cache[name] = ""
            except Exception:
                log.exception("Failed to apply theme '%s'", name)
                self._theme_qss_cache[name] = ""
        qss = self._theme_qss_cache.get(name, "")
        if qss and qss != self.styleSheet():
            self.setStyleSheet(qss)
        # Repaint all custom-drawn views with the new palette.
        for v in self._views.values():
            if v is self._current_view and hasattr(v, "refresh_theme"):
                v.refresh_theme()  # type: ignore[reportAttributeAccessIssue]
        # Sidebar mini-month uses hardcoded scene text — re-render it.
        if hasattr(self, "_sidebar"):
            self._sidebar._mini_month.render()  # type: ignore[reportPrivateUsage]

    # ── Zoom (Week/Day vertical pixel-per-hour) ──────────────────────────

    def _active_zoomable_view(self) -> WeekView | DayView | None:
        view = self._views.get(self._current_view_name)
        if isinstance(view, (WeekView, DayView)):
            return view
        return None

    def _zoom_in(self) -> None:
        v = self._active_zoomable_view()
        if v is not None:
            v.zoom_in()

    def _zoom_out(self) -> None:
        v = self._active_zoomable_view()
        if v is not None:
            v.zoom_out()

    def _zoom_reset(self) -> None:
        v = self._active_zoomable_view()
        if v is not None:
            v.zoom_reset()

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
            "1–4           Switch view (Month/Week/Day/Agenda)\n"
            "T             Go to today\n"
            "← / →         Previous / next period\n"
            "N             New event\n"
            "Ctrl+N        New event\n"
            "Ctrl+Shift+A  Quick add\n"
            "Ctrl+R        Refresh now\n"
            "Ctrl+Shift+R  Force full resync (clears sync state)\n"
            "Ctrl+,        Preferences\n"
            "Ctrl++ / Ctrl+-  Vertical zoom (Week/Day)\n"
            "Ctrl+0        Reset zoom\n"
            "Ctrl+scroll   Vertical zoom (Week/Day)\n"
            "F11           Toggle full-screen\n"
            "?             This help\n"
        )
        QMessageBox.information(self, "Keyboard shortcuts", help_text)

    # ── Async helpers ─────────────────────────────────────────────────────

    def _fire_async(self, coro, label: str) -> asyncio.Task[None]:
        """Schedule a coroutine and log any exception it raises."""
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: self._on_task_done(t, label))
        return task

    @staticmethod
    def _on_task_done(task: asyncio.Task[None], label: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Async task '%s' failed: %s", label, exc, exc_info=exc)

    async def _rebuild_instances_async(self) -> None:
        await asyncio.to_thread(self._store.rebuild_all_instances)

    # ── CalInfo cache ─────────────────────────────────────────────────────

    def _cal_info_provider(self) -> dict[str, CalInfo]:
        return self._cal_info

    def _account_meta_provider(self) -> dict[str, tuple[str, str, str]]:
        return self._account_meta

    def _build_cal_info_for_account(self, account_id: str):
        """Off-thread: return (new_cal_info_entries, acc_meta_tuple | None)."""
        acc = self._store.get_account(account_id)
        if acc is None:
            return {}, None
        acc_meta = (acc.display_name, acc.identity, acc.kind)
        cals = self._store.list_calendars(account_id, included_only=True)
        cal_info = {
            cal.id: CalInfo(
                id=cal.id,
                display_name=cal.display_name,
                color=cal.color,
                account_id=account_id,
                visible=bool(cal.is_visible),
                read_only=_is_read_only_cal(cal),
            )
            for cal in cals
        }
        return cal_info, acc_meta

    async def _rebuild_cal_info_for_account_async(self, account_id: str) -> None:
        """Rebuild cal_info for one account off-thread, then refresh sidebar + view."""
        try:
            new_entries, acc_meta = await asyncio.to_thread(
                self._build_cal_info_for_account, account_id
            )
        except Exception:
            log.exception("Failed to rebuild cal_info for account %s", account_id)
            return
        # Merge on GUI thread: drop old entries for this account, add new ones.
        updated = {
            k: v for k, v in self._cal_info.items() if v.account_id != account_id
        }
        updated.update(new_entries)
        self._cal_info = updated
        if acc_meta is not None:
            self._account_meta = {**self._account_meta, account_id: acc_meta}
        self._sidebar.refresh_for_account(account_id)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    def _on_cal_metadata_changed(self, calendar_id: str) -> None:
        """Patch _cal_info when a calendar's visibility, inclusion, or color changes."""
        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        if not cal.is_included:
            if calendar_id in self._cal_info:
                account_id = self._cal_info[calendar_id].account_id
                self._cal_info = {
                    k: v for k, v in self._cal_info.items() if k != calendar_id
                }
                self._sidebar.refresh_for_account(account_id)
                if self._current_view is not None and hasattr(
                    self._current_view, "refresh"
                ):
                    self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]
            return
        if calendar_id not in self._cal_info:
            # Newly re-included calendar — add it to the cache.
            self._cal_info = {
                **self._cal_info,
                calendar_id: CalInfo(
                    id=cal.id,
                    display_name=cal.display_name,
                    color=cal.color,
                    account_id=cal.account_id,
                    visible=bool(cal.is_visible),
                    read_only=_is_read_only_cal(cal),
                ),
            }
            self._sidebar.refresh_for_account(cal.account_id)
            if self._current_view is not None and hasattr(
                self._current_view, "refresh"
            ):
                self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]
            return
        old = self._cal_info[calendar_id]
        new_ci = CalInfo(
            id=old.id,
            display_name=cal.display_name,
            color=cal.color,
            account_id=old.account_id,
            visible=bool(cal.is_visible),
            read_only=_is_read_only_cal(cal),
        )
        self._cal_info = {**self._cal_info, calendar_id: new_ci}
        if new_ci.display_name != old.display_name:
            self._sidebar.refresh_for_account(old.account_id)

    # ── Sync signal handlers ──────────────────────────────────────────────

    def _account_label(self, account_id: str) -> str:
        return self._account_display_names.get(account_id, account_id)

    def _on_sync_started(self, account_id: str) -> None:
        self._syncing_accounts.add(account_id)
        self._sync_status.set_syncing(self._account_label(account_id))

    def _on_sync_progress(
        self, account_id: str, calendar_label: str, count: int
    ) -> None:
        label = f"{self._account_label(account_id)} / {calendar_label}"
        self._sync_status.set_syncing(f"{label} ({count} events)")

    def _on_sync_finished(self, account_id: str, n_changes: int) -> None:
        self._syncing_accounts.discard(account_id)
        label = self._account_label(account_id)
        self._sync_status.set_ok(f"{label} ({n_changes} changes)")
        # Rebuild cal_info for this account off-thread; the async helper then
        # refreshes sidebar and current view with accurate metadata.
        self._fire_async(
            self._rebuild_cal_info_for_account_async(account_id),
            f"rebuild_cal_info/{account_id}",
        )

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

    def _on_local_events_changed(self) -> None:
        self._local_events_pending = True

    def _on_events_changed(self, calendar_id: str, uids: set[str]) -> None:
        self._events_changed_calendars.add(calendar_id)
        self._events_changed_pending |= uids
        self._events_changed_timer.start()

    def _flush_events_changed(self) -> None:
        cals = self._events_changed_calendars
        local = self._local_events_pending
        self._events_changed_pending = set()
        self._events_changed_calendars = set()
        self._local_events_pending = False
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]
        self._update_range_label()
        if local:
            for cal_id in cals:
                cal = self._store.get_calendar(cal_id)
                if cal is not None:
                    self._sync.force_refresh(cal.account_id)
                    break

    # ── Account management ────────────────────────────────────────────────

    def _subscribe(self) -> None:
        from lilical.backends.subscription import (
            SUBSCRIPTION_ACCOUNT_ID,
            SUBSCRIPTION_ACCOUNT_NAME,
        )
        from lilical.ui.widgets.subscribe_dialog import SubscribeDialog

        dlg = SubscribeDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        canonical_source = dlg.canonical_source
        display_name = dlg.display_name
        color = dlg.color
        events = dlg.events
        content_sha256 = dlg.content_sha256

        self._sync_status.set_syncing("Importing subscription…")

        async def _do_import() -> None:
            try:
                await asyncio.to_thread(
                    self._store.create_subscription,
                    canonical_source=canonical_source,
                    display_name=display_name,
                    color=color,
                    events=events,
                    content_sha256=content_sha256,
                    rebuild_batch_size=0,
                )
            except Exception:
                log.exception("failed to create subscription")
                self._sync_status.set_ready()
                QMessageBox.critical(
                    self, "Subscription failed", "Could not save subscription."
                )
                return
            self._sync_status.set_ready()
            # Subscriptions account may have just been created — refresh metadata.
            if SUBSCRIPTION_ACCOUNT_ID not in self._account_meta:
                self._account_display_names[SUBSCRIPTION_ACCOUNT_ID] = (
                    SUBSCRIPTION_ACCOUNT_NAME
                )
                self._account_meta[SUBSCRIPTION_ACCOUNT_ID] = (
                    SUBSCRIPTION_ACCOUNT_NAME,
                    "",
                    "subscription",
                )
                self._fire_async(
                    self._sync.start_account(SUBSCRIPTION_ACCOUNT_ID),
                    f"start_account/{SUBSCRIPTION_ACCOUNT_ID}",
                )
            self._fire_async(
                self._rebuild_cal_info_for_account_async(SUBSCRIPTION_ACCOUNT_ID),
                f"rebuild_cal_info/{SUBSCRIPTION_ACCOUNT_ID}",
            )

        self._fire_async(_do_import(), "subscribe/import")

    def _on_refresh_calendar(self, calendar_id: str) -> None:
        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        self._sync.force_refresh(cal.account_id)
        self._sync_status.set_syncing(self._account_label(cal.account_id))

    def _on_unsubscribe(self, calendar_id: str) -> None:
        from lilical.backends.subscription import SUBSCRIPTION_ACCOUNT_ID

        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Unsubscribe?")
        confirm.setText(f'Unsubscribe from "{cal.display_name}"?')
        confirm.setInformativeText(
            "All locally-cached events for this subscription will be removed. "
            "The source itself is not affected."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            account_gone = self._store.delete_subscription(calendar_id)
        except Exception:
            log.exception("failed to delete subscription")
            QMessageBox.critical(
                self, "Unsubscribe failed", "Could not remove subscription."
            )
            return
        self._cal_info.pop(calendar_id, None)
        if account_gone:
            self._fire_async(
                self._sync.stop_account(SUBSCRIPTION_ACCOUNT_ID),
                f"stop_account/{SUBSCRIPTION_ACCOUNT_ID}",
            )
            self._account_display_names.pop(SUBSCRIPTION_ACCOUNT_ID, None)
            self._account_meta.pop(SUBSCRIPTION_ACCOUNT_ID, None)
            self._sidebar.refresh()
        else:
            self._fire_async(
                self._rebuild_cal_info_for_account_async(SUBSCRIPTION_ACCOUNT_ID),
                f"rebuild_cal_info/{SUBSCRIPTION_ACCOUNT_ID}",
            )
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    def _add_account(self) -> None:
        dlg = AccountSetupDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:  # type: ignore[reportAttributeAccessIssue]
            return
        data = dlg.result_data()
        if data is None:
            return
        kind, display_name, identity, server_url, secret_data, include_contacts = data
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
            include_contacts=include_contacts,
        )
        label = display_name or identity or ""
        self._account_display_names[account_id] = label
        self._account_meta[account_id] = (label, identity or "", kind or "")
        self._sidebar.refresh()
        self._fire_async(
            self._sync.start_account(account_id), f"start_account/{account_id}"
        )

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
        self._account_display_names[account_id] = new_name
        if account_id in self._account_meta:
            old = self._account_meta[account_id]
            self._account_meta[account_id] = (new_name, old[1], old[2])
        self._sidebar.refresh()

    def _on_rename_calendar(self, calendar_id: str) -> None:
        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename calendar",
            "Calendar name:",
            QLineEdit.EchoMode.Normal,
            cal.display_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == cal.display_name:
            return
        self._fire_async(
            self._rename_calendar_async(
                cal.id, cal.account_id, cal.provider_id, new_name
            ),
            f"rename_calendar/{calendar_id}",
        )

    async def _rename_calendar_async(
        self, calendar_id: str, account_id: str, provider_id: str, new_name: str
    ) -> None:
        from lilical.backends.base import AuthExpired, PermanentError, TransientError

        acc = await asyncio.to_thread(self._store.get_account, account_id)
        if acc is None:
            return
        backend = await asyncio.to_thread(lambda: self._backend_factory(acc))
        try:
            await backend.rename_calendar(provider_id, new_name)
        except (AuthExpired, PermanentError, TransientError) as exc:
            msg = str(exc) or repr(exc)
            QMessageBox.warning(
                self, "Rename failed", f"Could not rename calendar:\n\n{msg}"
            )
            return
        await asyncio.to_thread(
            self._store.set_calendar_display_name, calendar_id, new_name
        )

    def _on_new_calendar(self, account_id: str) -> None:
        name, ok = QInputDialog.getText(
            self,
            "New calendar",
            "Calendar name:",
            QLineEdit.EchoMode.Normal,
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        self._fire_async(
            self._create_calendar_async(account_id, name),
            f"create_calendar/{account_id}",
        )

    async def _create_calendar_async(self, account_id: str, name: str) -> None:
        from lilical.backends.base import AuthExpired, PermanentError, TransientError

        acc = await asyncio.to_thread(self._store.get_account, account_id)
        if acc is None:
            return
        backend = await asyncio.to_thread(lambda: self._backend_factory(acc))
        try:
            new_cal = await backend.create_calendar(name)
        except (AuthExpired, PermanentError, TransientError) as exc:
            msg = str(exc) or repr(exc)
            QMessageBox.warning(
                self, "Create calendar failed", f"Could not create calendar:\n\n{msg}"
            )
            return
        await asyncio.to_thread(
            self._store.upsert_calendars, account_id, [new_cal]
        )
        await self._rebuild_cal_info_for_account_async(account_id)
        self._sync.force_refresh(account_id)

    def _on_delete_calendar(self, calendar_id: str) -> None:
        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        if cal.is_primary:
            QMessageBox.information(
                self,
                "Cannot delete calendar",
                "The primary calendar for an account cannot be deleted.",
            )
            return
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete calendar?")
        confirm.setText(f'Delete calendar "{cal.display_name}"?')
        confirm.setInformativeText(
            "This will remove all locally-cached events for this calendar. "
            "Events stored on the provider's servers will not be affected."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        self._fire_async(
            self._delete_calendar_async(cal.id, cal.account_id, cal.provider_id),
            f"delete_calendar/{calendar_id}",
        )

    async def _delete_calendar_async(
        self, calendar_id: str, account_id: str, provider_id: str
    ) -> None:
        from lilical.backends.base import AuthExpired, PermanentError, TransientError

        acc = await asyncio.to_thread(self._store.get_account, account_id)
        if acc is None:
            return
        backend = await asyncio.to_thread(lambda: self._backend_factory(acc))
        try:
            await backend.delete_calendar(provider_id)
        except (AuthExpired, PermanentError, TransientError) as exc:
            msg = str(exc) or repr(exc)
            QMessageBox.warning(
                self, "Delete failed", f"Could not delete calendar:\n\n{msg}"
            )
            return
        await asyncio.to_thread(self._store.delete_calendar, calendar_id)
        self._cal_info.pop(calendar_id, None)
        await self._rebuild_cal_info_for_account_async(account_id)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    def _on_change_calendar_color(self, calendar_id: str) -> None:
        cal = self._store.get_calendar(calendar_id)
        if cal is None:
            return
        initial = QColor(cal.color or "#5e9fff")
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
        if new_hex == (cal.color or "").lower():
            return
        self._store.set_calendar_color(calendar_id, new_hex)
        chip = self._sidebar._chips.get(calendar_id)  # type: ignore[reportPrivateUsage]
        if chip is not None:
            chip.update_color(new_hex)

    def _on_reauth_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        dlg = AccountSetupDialog(self, existing_account=acc)
        if dlg.exec() != QDialog.DialogCode.Accepted:  # type: ignore[reportAttributeAccessIssue]
            return
        data = dlg.result_data()
        if data is None:
            return
        _kind, display_name, identity, server_url, secret_data, include_contacts = data
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
            include_contacts=include_contacts,
        )
        self._sidebar.refresh()
        self._fire_async(
            self._restart_account_sync(account_id), f"restart_sync/{account_id}"
        )
        # Clear any auth-expired warning
        self._sync_status.set_ready()

    def _on_choose_calendars(self, account_id: str) -> None:
        from lilical.ui.widgets.calendar_picker import CalendarPickerDialog

        dlg = CalendarPickerDialog(self, account_id, self._store)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # Rebuild cal_info off-thread (dialog may have toggled visibility); the
        # async helper refreshes sidebar + view once the cache is updated.
        self._fire_async(
            self._rebuild_cal_info_for_account_async(account_id),
            f"rebuild_cal_info/{account_id}",
        )

    def _on_sync_now_account(self, account_id: str) -> None:
        self._sync.force_refresh(account_id)
        self._sync_status.set_syncing(self._account_label(account_id))

    def _on_delete_account(self, account_id: str) -> None:
        acc = self._store.get_account(account_id)
        if acc is None:
            return
        cals = self._store.list_calendars(account_id, included_only=False)
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
        self._account_display_names.pop(account_id, None)
        self._account_meta.pop(account_id, None)
        self._cal_info = {
            k: v for k, v in self._cal_info.items() if v.account_id != account_id
        }
        self._sidebar.refresh()
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]
        self._sync_status.set_ready()

    async def _restart_account_sync(self, account_id: str) -> None:
        await self._sync.stop_account(account_id)
        await self._sync.start_account(account_id)

    def _on_calendar_visibility_changed(
        self, calendar_id: str, is_visible: bool
    ) -> None:
        try:
            self._store.set_calendar_visibility(calendar_id, is_visible)
        except Exception:
            log.exception("Failed to update calendar visibility for %s", calendar_id)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    def _on_calendar_color_changed(self, _calendar_id: str, _new_hex: str) -> None:
        # The swatch already persisted via store.set_calendar_color. Just kick
        # the current view to re-paint its chips with the new fallback color.
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    def _on_account_order_changed(self) -> None:
        self._account_meta.clear()
        for acc in self._store.list_accounts():
            self._account_meta[acc.id] = (acc.display_name, acc.identity, acc.kind)
        self._sidebar.refresh()

    def _on_calendar_order_changed(self, account_id: str) -> None:
        new_entries, _ = self._build_cal_info_for_account(account_id)
        self._cal_info = {
            k: v for k, v in self._cal_info.items() if v.account_id != account_id
        }
        self._cal_info.update(new_entries)
        self._sidebar.refresh_for_account(account_id)
        if self._current_view is not None and hasattr(self._current_view, "refresh"):
            self._current_view.refresh()  # type: ignore[reportAttributeAccessIssue]

    # ── Window lifecycle ──────────────────────────────────────────────────

    @override
    def closeEvent(self, e) -> None:
        # Closing the window quits the app. Use the tray icon's Show/Quit to
        # keep it running in the background instead.
        super().closeEvent(e)
        QApplication.quit()
