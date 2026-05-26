"""Shared helpers for edit/delete actions on recurring events.

All four views (month, week, day, agenda) use these two functions instead of
duplicating the RecurrenceActionDialog dispatch logic in each view.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox, QWidget

if TYPE_CHECKING:
    from lilical.models.event import Event
    from lilical.storage.event_store import EventStore


def open_details_dialog(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
) -> None:
    """Show read-only event details; routes to edit/delete flows on user request."""
    from lilical.ui.widgets.event_details_dialog import EventDetailsDialog

    dlg = EventDetailsDialog(
        parent, store=store, event=event, instance_dtstart=instance_dtstart
    )
    if not dlg.exec():
        return
    if dlg.response_choice is not None:
        master = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                master = m
        store.queue_respond(master.uid, master.calendar_id, dlg.response_choice)
        return
    if dlg.delete_requested:
        open_delete_dialog(parent, store, event, instance_dtstart)
    elif dlg.edit_requested:
        open_edit_dialog(parent, store, event, instance_dtstart)


def open_edit_dialog(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
) -> None:
    """Full edit flow: recurrence scope prompt → EventDialog → dispatch to store.

    For non-recurring events the recurrence dialog is skipped.
    """
    from lilical.ui.widgets.event_dialog import EventDialog
    from lilical.ui.widgets.recurrence_action_dialog import RecurrenceActionDialog

    is_recurring = bool(event.rrule or event.recurrence_id is not None)
    choice = "series"

    if is_recurring:
        rad = RecurrenceActionDialog(parent, action="edit")
        if not rad.exec():
            return
        choice = rad.choice or "series"

    # Resolve which event data to show in the dialog
    if choice == "occurrence":
        edit_event = event  # show this occurrence's data (may be an override)
    else:
        # For "series" and "following", show the master
        if event.recurrence_id is not None:
            master = store.get_event(event.uid, event.calendar_id)
            edit_event = master if master else event
        else:
            edit_event = event

    dlg = EventDialog(parent, store=store, event=edit_event)
    if not dlg.exec():
        return

    if dlg.delete_requested:
        _dispatch_delete(parent, store, event, instance_dtstart, choice)
        return

    cal_id = dlg.calendar_id or edit_event.calendar_id
    edited = dataclasses.replace(dlg.build_event(edit_event.uid), calendar_id=cal_id)
    _dispatch_edit(parent, store, event, instance_dtstart, edited, choice)


def open_copy_dialog(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
) -> None:
    """Full copy flow: recurrence scope prompt → calendar picker → queue_copy.

    For non-recurring events the recurrence dialog is skipped.
    'following' copies the master's series starting at the clicked occurrence
    date with the rrule intact (no count/until adjustment).
    """
    from lilical.ui.widgets.copy_to_calendar_dialog import CopyToCalendarDialog
    from lilical.ui.widgets.recurrence_action_dialog import RecurrenceActionDialog

    is_recurring = bool(event.rrule or event.recurrence_id is not None)
    choice = "series"

    if is_recurring:
        rad = RecurrenceActionDialog(parent, action="copy")
        if not rad.exec():
            return
        choice = rad.choice or "series"

    # Build the event to copy based on scope choice
    if choice == "occurrence":
        # Resolve to a one-off: strip recurrence, anchor to the clicked date
        if event.recurrence_id is not None:
            # Already an override row — use it directly but strip recurrence fields
            src = dataclasses.replace(
                event,
                rrule=None,
                exdates=(),
                rdates=(),
                recurrence_id=None,
            )
        elif instance_dtstart is not None:
            # Regular occurrence of a repeating master — build a one-off
            has_bounds = event.dtend and event.dtstart
            delta = (event.dtend - event.dtstart) if has_bounds else None  # type: ignore[operator]
            src = dataclasses.replace(
                event,
                dtstart=instance_dtstart,
                dtend=(instance_dtstart + delta) if delta is not None else event.dtend,
                rrule=None,
                exdates=(),
                rdates=(),
                recurrence_id=None,
            )
        else:
            src = event
    elif choice == "following":
        # Copy master series starting at the clicked occurrence date
        master = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                master = m
        anchor = instance_dtstart or event.recurrence_id or master.dtstart
        if anchor is not None and master.dtstart is not None:
            has_bounds = master.dtend and master.dtstart
            delta = (master.dtend - master.dtstart) if has_bounds else None  # type: ignore[operator]
            src = dataclasses.replace(
                master,
                dtstart=anchor,
                dtend=(anchor + delta) if delta is not None else master.dtend,
                exdates=(),
                rdates=(),
                recurrence_id=None,
            )
        else:
            src = master
    else:
        # Entire series — copy the master
        src = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                src = m

    dlg = CopyToCalendarDialog(parent, store=store, source_calendar_id=src.calendar_id)
    if not dlg.exec():
        return
    target_id = dlg.calendar_id
    if not target_id:
        return
    store.queue_copy(src, target_id)


def open_delete_dialog(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
) -> None:
    """Full delete flow: recurrence scope prompt → dispatch to store."""
    from lilical.ui.widgets.recurrence_action_dialog import RecurrenceActionDialog

    is_recurring = bool(event.rrule or event.recurrence_id is not None)
    choice = "series"

    if is_recurring:
        rad = RecurrenceActionDialog(parent, action="delete")
        if not rad.exec():
            return
        choice = rad.choice or "series"

    _dispatch_delete(parent, store, event, instance_dtstart, choice)


def _dispatch_edit(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
    edited: "Event",
    choice: str,
) -> None:
    if choice == "occurrence":
        if instance_dtstart is None and event.recurrence_id is None:
            QMessageBox.warning(
                parent, "Edit failed", "Could not determine occurrence date."
            )
            return
        rid = (
            event.recurrence_id if event.recurrence_id is not None else instance_dtstart
        )
        store.queue_update_instance(
            uid=event.uid,
            calendar_id=edited.calendar_id,
            recurrence_id_dt=rid,  # type: ignore[reportArgumentType]
            edited=edited,
        )
    elif choice == "following":
        if instance_dtstart is None and event.recurrence_id is None:
            QMessageBox.warning(
                parent, "Edit failed", "Could not determine split date."
            )
            return
        split_at = (
            event.recurrence_id if event.recurrence_id is not None else instance_dtstart
        )
        store.queue_split_series(
            uid=event.uid,
            calendar_id=event.calendar_id,
            split_at_dt=split_at,  # type: ignore[reportArgumentType]
            edited_event_for_tail=edited,
        )
    else:
        # Entire series
        master = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                master = m
        updated = dataclasses.replace(
            edited,
            uid=master.uid,
            calendar_id=edited.calendar_id or master.calendar_id,
            etag=master.etag,
            sequence=(master.sequence or 0) + 1,
        )
        if updated.calendar_id != master.calendar_id:
            store.queue_move(
                uid=master.uid,
                old_calendar_id=master.calendar_id,
                new_calendar_id=updated.calendar_id,
                moved_event=updated,
            )
        else:
            store.queue_update(updated, master.etag)


def _dispatch_delete(
    parent: QWidget,
    store: "EventStore",
    event: "Event",
    instance_dtstart: datetime | None,
    choice: str,
) -> None:
    if choice == "occurrence":
        if instance_dtstart is None and event.recurrence_id is None:
            QMessageBox.warning(
                parent, "Delete failed", "Could not determine occurrence date."
            )
            return
        rid = (
            event.recurrence_id if event.recurrence_id is not None else instance_dtstart
        )
        store.queue_delete_instance(
            uid=event.uid,
            calendar_id=event.calendar_id,
            recurrence_id_dt=rid,  # type: ignore[reportArgumentType]
        )
    elif choice == "following":
        if instance_dtstart is None and event.recurrence_id is None:
            QMessageBox.warning(
                parent, "Delete failed", "Could not determine split date."
            )
            return
        split_at = (
            event.recurrence_id if event.recurrence_id is not None else instance_dtstart
        )
        master = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                master = m
        store.queue_truncate_series(
            uid=master.uid,
            calendar_id=master.calendar_id,
            until_dt=split_at,  # type: ignore[reportArgumentType]
        )
    else:
        master = event
        if event.recurrence_id is not None:
            m = store.get_event(event.uid, event.calendar_id)
            if m:
                master = m
        if (
            QMessageBox.question(
                parent,
                "Delete event",
                f'Delete "{master.summary}"?'
                + (" (entire series)" if master.rrule else ""),
            )
            == QMessageBox.StandardButton.Yes
        ):
            store.queue_delete(master.uid, master.calendar_id)
