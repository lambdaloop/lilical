from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import override

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHeaderView,
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


def _query_agenda_data(
    store,
    start: date,
    end: date,
    cal_info_snap: dict,
    current_snapshot: frozenset,
    snapshot_start: "date | None",
) -> dict | None:
    """Off-thread: query DB and check snapshot. Returns None if unchanged/error."""
    start_dt = _local_midnight(start)
    end_dt = _local_midnight(end)
    visible_ids = {ci.id for ci in cal_info_snap.values() if ci.visible}
    try:
        instances = store.list_instances(start_dt, end_dt, calendar_ids=visible_ids)
    except Exception:
        log.exception("AgendaView: failed to query instances")
        return None
    new_snapshot = frozenset((i.uid, i.dtstart_local, i.calendar_id) for i in instances)
    if new_snapshot == current_snapshot and snapshot_start == start:
        return None
    events = store.events_for_instances(instances)
    completions = store.completion_for_instances(instances)
    cal_info: dict[str, tuple[str, str | None]] = {
        ci.id: (ci.display_name, ci.color) for ci in cal_info_snap.values()
    }
    return {
        "instances": instances,
        "events": events,
        "cal_info": cal_info,
        "snapshot": new_snapshot,
        "start": start,
        "end": end,
        "completions": completions,
    }


class AgendaView(QWidget):
    def __init__(self, store: EventStore, cal_info_provider=None) -> None:
        super().__init__()
        self._store = store
        self._cal_info_provider = cal_info_provider or (lambda: {})
        self._start = date.today()
        self._snapshot: frozenset[tuple] = frozenset()
        self._snapshot_start: "date | None" = None
        self._refresh_task: asyncio.Task | None = None
        self._time_format = "24h"
        self._completed_enabled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Done", "Time", "Event", "Calendar"])
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setColumnHidden(0, True)
        self._tree.setUniformRowHeights(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._tree)

        self._store.instance_completion_changed.connect(self._on_completion_changed)

    def navigate(self, days: int) -> None:
        self._start = self._start + timedelta(days=days)
        self.refresh()

    def go_today(self) -> None:
        self._start = date.today()
        self.refresh()

    def go_to_date(self, d: date) -> None:
        self._start = d
        self.refresh()

    @override
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._snapshot_start is None:
            self.refresh()

    def set_time_format(self, fmt: str) -> None:
        if fmt == self._time_format:
            return
        self._time_format = fmt
        self._snapshot_start = None
        self.refresh()

    def set_completed_events_enabled(self, enabled: bool) -> None:
        if enabled == self._completed_enabled:
            return
        self._completed_enabled = enabled
        self._tree.setColumnHidden(0, not enabled)
        self._snapshot_start = None
        self.refresh()

    def _on_completion_changed(
        self, _cal_id: str, _uid: str, _dtstart_utc: int
    ) -> None:
        self._snapshot_start = None
        self.refresh()

    def refresh_theme(self) -> None:
        # Force full rebuild on theme change to repaint day-header backgrounds.
        self._snapshot_start = None
        self.refresh()

    def range_label(self) -> str:
        end = self._start + timedelta(days=_DAYS_AHEAD - 1)
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self, *, data_dirty: bool = True) -> None:
        if not data_dirty:
            return  # no geometry to recompute
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        start = self._start
        end = start + timedelta(days=_DAYS_AHEAD)
        cal_info_snap = self._cal_info_provider()
        self._refresh_task = asyncio.ensure_future(
            self._refresh_async(
                start, end, self._snapshot, self._snapshot_start, cal_info_snap
            )
        )

    async def _refresh_async(
        self,
        start: date,
        end: date,
        snapshot: frozenset,
        snapshot_start: "date | None",
        cal_info_snap: dict,
    ) -> None:
        try:
            plan = await asyncio.to_thread(
                _query_agenda_data,
                self._store,
                start,
                end,
                cal_info_snap,
                snapshot,
                snapshot_start,
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
        completions: frozenset = plan.get("completions", frozenset())

        self._snapshot = plan["snapshot"]
        self._snapshot_start = start

        blocker = QSignalBlocker(self._tree)
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
            empty.setText(1, "No events in this period")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            del blocker
            return

        for d in sorted(by_day):
            day_item = QTreeWidgetItem(self._tree)
            day_item.setText(1, d.strftime("%A, %B %-d, %Y"))
            day_item.setFont(1, bold)
            day_item.setBackground(0, QColor(theme.BG_SURFACE_3))
            day_item.setBackground(1, QColor(theme.BG_SURFACE_3))
            day_item.setBackground(2, QColor(theme.BG_SURFACE_3))
            day_item.setBackground(3, QColor(theme.BG_SURFACE_3))
            day_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

            tfmt = "%-I:%M %p" if self._time_format == "12h" else "%H:%M"
            for t, inst in sorted(by_day[d], key=lambda x: (not x[1].all_day, x[0])):  # type: ignore[reportAttributeAccessIssue]
                event = events.get(id(inst))
                if event is None:
                    continue
                is_completed = (
                    inst.calendar_id, inst.uid, inst.dtstart_utc  # type: ignore[reportAttributeAccessIssue]
                ) in completions

                row = QTreeWidgetItem(day_item)
                if self._completed_enabled:
                    row.setFlags(
                        row.flags()
                        | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsEnabled
                    )
                    state = (
                        Qt.CheckState.Checked
                        if is_completed
                        else Qt.CheckState.Unchecked
                    )
                    row.setCheckState(0, state)
                    row.setData(
                        0,
                        Qt.ItemDataRole.UserRole + 1,
                        (inst.calendar_id, inst.uid, inst.dtstart_utc),  # type: ignore[reportAttributeAccessIssue]
                    )
                if inst.all_day:  # type: ignore[reportAttributeAccessIssue]
                    row.setText(1, "All day")
                else:
                    try:
                        end_t = datetime.fromisoformat(inst.dtend_local).astimezone()  # type: ignore[reportAttributeAccessIssue]
                        row.setText(1, f"{t.strftime(tfmt)} – {end_t.strftime(tfmt)}")
                    except (ValueError, TypeError, AttributeError):
                        row.setText(1, t.strftime(tfmt))
                row.setText(2, event.summary or "(no title)")

                cal_name, cal_color = cal_info.get(
                    inst.calendar_id, (inst.calendar_id, None)  # type: ignore[reportAttributeAccessIssue]
                )
                row.setText(3, cal_name)

                color_hint = event.color or cal_color
                row.setIcon(2, _color_swatch_icon(color_hint))

                row.setData(0, Qt.ItemDataRole.UserRole, (inst, t))
                self._style_completed_row(row, is_completed)

            day_item.setExpanded(True)
        del blocker

    def _style_completed_row(self, row: QTreeWidgetItem, completed: bool) -> None:
        """Apply or clear muted/strikethrough styling for a completed event row."""
        if not self._completed_enabled or not completed:
            return
        muted = QColor(theme.TEXT_DISABLED)
        strike_font = QFont()
        strike_font.setStrikeOut(True)
        for col in (1, 2, 3):
            row.setForeground(col, muted)
        row.setFont(2, strike_font)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or not self._completed_enabled:
            return
        key_data = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if key_data is None:
            return
        cal_id, uid, dtstart_utc = key_data
        completed = item.checkState(0) == Qt.CheckState.Checked
        self._store.set_completed(cal_id, uid, dtstart_utc, completed)
        self._style_completed_row(item, completed)

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
