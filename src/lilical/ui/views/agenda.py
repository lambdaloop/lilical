from __future__ import annotations

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


class AgendaView(QWidget):
    def __init__(self, store: EventStore) -> None:
        super().__init__()
        self._store = store
        self._start = date.today()

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
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._tree)

        self.refresh()

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
        self.refresh()

    def range_label(self) -> str:
        end = self._start + timedelta(days=_DAYS_AHEAD - 1)
        return f"{self._start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    def refresh(self) -> None:
        self._tree.clear()

        end = self._start + timedelta(days=_DAYS_AHEAD)
        start_dt = _local_midnight(self._start)
        end_dt = _local_midnight(end)

        try:
            instances = self._store.list_instances(
                start_dt, end_dt, calendar_ids=self._store.visible_calendar_ids()
            )
        except Exception:
            log.exception("AgendaView: failed to query instances")
            return

        by_day: dict[date, list[tuple[datetime, object]]] = {}
        for inst in instances:
            try:
                t = datetime.fromisoformat(inst.dtstart_local).astimezone()
            except (ValueError, TypeError):
                continue
            d = t.date()
            if d < self._start or d >= end:
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

            for t, inst in sorted(by_day[d], key=lambda x: (not x[1].all_day, x[0])):
                event = self._store.get_event_for_instance(inst)
                if event is None:
                    continue
                row = QTreeWidgetItem(day_item)
                if inst.all_day:
                    row.setText(0, "All day")
                else:
                    row.setText(0, t.strftime("%H:%M"))
                row.setText(1, event.summary or "(no title)")

                # Show calendar name if available
                cal_label = inst.calendar_id
                accs = self._store.list_accounts()
                for acc in accs:
                    for cal in self._store.list_calendars(acc.id, visible_only=False):
                        if cal.id == inst.calendar_id:
                            cal_label = cal.display_name
                            break
                row.setText(2, cal_label)

                # Color swatch icon to the left of the event title.
                color_hint = event.color
                if not color_hint:
                    cal = self._store.get_calendar(inst.calendar_id)
                    color_hint = cal.color if cal else None
                row.setIcon(1, _color_swatch_icon(color_hint))

                row.setData(0, Qt.ItemDataRole.UserRole, (inst, t))

            day_item.setExpanded(True)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        inst, instance_dtstart = data
        event = self._store.get_event_for_instance(inst)
        if event is None:
            return
        from lilical.ui.views._recurrence_actions import open_edit_dialog
        open_edit_dialog(self, self._store, event, instance_dtstart)

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
