from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lilical.storage.event_store import EventStore
from lilical.ui import theme


def _color_swatch_icon(color_hex: str | None, size: int = 12) -> QIcon:
    """Build a small filled-circle icon used as the row's color chip."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color_hex) if color_hex else QColor(theme.CHIP_FALLBACK)
    if not c.isValid():
        c = QColor(theme.CHIP_FALLBACK)
    p.setBrush(c)
    p.setPen(QPen(c.darker(160), 1))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


log = logging.getLogger(__name__)

_DAYS_AHEAD = 30


def _local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0).astimezone()


def _build_agenda_plan(
    store,
    start: date,
    end: date,
    current_snapshot: frozenset,
    snapshot_start: "date | None",
) -> dict | None:
    """Off-thread: query DB and check snapshot. Returns None if unchanged/error."""
    start_dt = _local_midnight(start)
    end_dt = _local_midnight(end)
    try:
        instances = store.list_instances(
            start_dt, end_dt, calendar_ids=store.visible_calendar_ids()
        )
    except Exception:
        log.exception("AgendaView: failed to query instances")
        return None
    new_snapshot = frozenset(
        (i.uid, i.dtstart_local, i.calendar_id) for i in instances
    )
    if new_snapshot == current_snapshot and snapshot_start == start:
        return None
    events = store.events_for_instances(instances)
    cal_info: dict[str, tuple[str, str | None]] = {}
    for acc in store.list_accounts():
        for cal in store.list_calendars(acc.id, visible_only=False):
            cal_info[cal.id] = (cal.display_name, cal.color)
    return {
        "instances": instances,
        "events": events,
        "cal_info": cal_info,
        "snapshot": new_snapshot,
        "start": start,
        "end": end,
    }


class AgendaView(QWidget):
    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        self._start = date.today()
        self._snapshot: frozenset[tuple] = frozenset()
        self._snapshot_start: "date | None" = None
        self._refresh_task: asyncio.Task | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Time", "Event", "Calendar"])
        self._tree.header().setStretchLastSection(True)
        self._tree.setUniformRowHeights(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._tree)

    def navigate(self, days: int) -> None:
        self._start = self._start + timedelta(days=days)
        self.refresh()

    def go_today(self) -> None:
        self._start = date.today()
        self.refresh()

    def go_to_date(self, d: date) -> None:
        self._start = d
        self.refresh()

    def refresh_theme(self) -> None:
        # Force full rebuild on theme change to repaint day-header backgrounds.
        self._snapshot_start = None
        self.refresh()

    def range_label(self) -> str:
        end = self._start + timedelta(days=_DAYS_AHEAD - 1)
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        start = self._start
        end = start + timedelta(days=_DAYS_AHEAD)
        self._refresh_task = asyncio.ensure_future(
            self._refresh_async(start, end, self._snapshot, self._snapshot_start)
        )

    async def _refresh_async(
        self,
        start: date,
        end: date,
        snapshot: frozenset,
        snapshot_start: "date | None",
    ) -> None:
        try:
            plan = await asyncio.to_thread(
                _build_agenda_plan, self._store, start, end, snapshot, snapshot_start
            )
        except asyncio.CancelledError:
            return
        if plan is None:
            return
        self._apply_plan(plan)

    def _apply_plan(self, plan: dict) -> None:
        instances = plan["instances"]
        events = plan["events"]
        cal_info: dict[str, tuple[str, str | None]] = plan["cal_info"]
        start: date = plan["start"]
        end: date = plan["end"]

        self._snapshot = plan["snapshot"]
        self._snapshot_start = start

        self._tree.clear()

        by_day: dict[date, list[tuple[datetime, object]]] = {}
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            d = t.date()
            if d < start or d >= end:
                continue
            by_day.setdefault(d, []).append((t, inst))

        bold = QFont()
        bold.setBold(True)

        if not by_day:
            empty = QTreeWidgetItem(self._tree)
            empty.setText(0, "No events in this period")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            return

        for d in sorted(by_day):
            day_item = QTreeWidgetItem(self._tree)
            day_item.setText(0, d.strftime("%A, %B %-d, %Y"))
            day_item.setFont(0, bold)
            day_item.setBackground(0, QColor(theme.BG_SURFACE_3))
            day_item.setBackground(1, QColor(theme.BG_SURFACE_3))
            day_item.setBackground(2, QColor(theme.BG_SURFACE_3))
            day_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

            for t, inst in sorted(by_day[d], key=lambda x: (not x[1].all_day, x[0])):  # type: ignore[reportAttributeAccessIssue]
                event = events.get(id(inst))
                if event is None:
                    continue
                row = QTreeWidgetItem(day_item)
                if inst.all_day:  # type: ignore[reportAttributeAccessIssue]
                    row.setText(0, "All day")
                else:
                    row.setText(0, t.strftime("%H:%M"))
                row.setText(1, event.summary or "(no title)")

                cal_name, cal_color = cal_info.get(inst.calendar_id, (inst.calendar_id, None))  # type: ignore[reportAttributeAccessIssue]
                row.setText(2, cal_name)

                color_hint = event.color or cal_color
                row.setIcon(1, _color_swatch_icon(color_hint))

                row.setData(0, Qt.ItemDataRole.UserRole, (inst, t))

            day_item.setExpanded(True)

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        inst, instance_dtstart = data
        event = self._store.get_event_for_instance(inst)
        if event is None:
            return
        from lilical.ui.views._recurrence_actions import open_details_dialog

        open_details_dialog(self, self._store, event, instance_dtstart)

    def _on_context_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        item = self._tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        inst, instance_dtstart = data
        event = self._store.get_event_for_instance(inst)
        if event is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if action == edit_action:
            from lilical.ui.views._recurrence_actions import open_edit_dialog

            open_edit_dialog(self, self._store, event, instance_dtstart)
        elif action == delete_action:
            from lilical.ui.views._recurrence_actions import open_delete_dialog

            open_delete_dialog(self, self._store, event, instance_dtstart)
