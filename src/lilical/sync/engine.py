from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any

from PySide6.QtCore import QObject, Signal

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    PermanentError,
    TransientError,
)
from lilical.storage.event_store import EventStore
from lilical.storage.secrets import SecretsStore

log = logging.getLogger(__name__)


def _next_backoff(prev: int) -> int:
    base = min(max(prev * 2, 5), 300)
    return int(base * random.uniform(0.5, 1.5))


class SyncEngine(QObject):
    sync_started = Signal(str)
    sync_progress = Signal(str, str, int)  # account_id, calendar_label, events_so_far
    sync_finished = Signal(str, int)
    sync_failed = Signal(str, str)
    auth_expired = Signal(str, str)  # account_id, error message
    conflict_detected = Signal(str)

    def __init__(self, store: EventStore, secrets: SecretsStore, factory) -> None:
        super().__init__()
        self._store = store
        self._secrets = secrets
        self._factory = factory
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._wake_events: dict[str, asyncio.Event] = {}

    async def start_all(self) -> None:
        for acc in await asyncio.to_thread(self._store.list_accounts, True):
            if acc.id not in self._tasks:
                self._tasks[acc.id] = asyncio.create_task(self._run_account(acc))

    async def start_account(self, account_id: str) -> None:
        if account_id in self._tasks:
            return
        acc = await asyncio.to_thread(self._store.get_account, account_id)
        if acc is None:
            return
        self._wake_events[account_id] = asyncio.Event()
        self._tasks[account_id] = asyncio.create_task(self._run_account(acc))

    async def stop_all(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def stop_account(self, account_id: str) -> None:
        task = self._tasks.get(account_id)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._tasks.pop(account_id, None)
        self._wake_events.pop(account_id, None)

    def force_refresh(self, account_id: str) -> None:
        ev = self._wake_events.get(account_id)
        if ev is not None:
            ev.set()
            return
        # No wake event → the sync loop has exited (e.g. terminated by
        # AuthExpired). Resurrect it so the user's "Sync now" / Ctrl+R
        # actually retries instead of being a silent no-op.
        if account_id in self._tasks:
            # Task is mid-teardown; let it finish.
            return
        asyncio.get_event_loop().create_task(self._resurrect_account(account_id))

    async def _resurrect_account(self, account_id: str) -> None:
        acc = await asyncio.to_thread(self._store.get_account, account_id)
        if acc is None:
            return
        self._tasks[account_id] = asyncio.create_task(self._run_account(acc))

    async def _run_account(self, account) -> None:
        backend = await asyncio.to_thread(self._factory, account)
        wake = self._wake_events[account.id] = asyncio.Event()
        delay = 0
        try:
            while True:
                if delay:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=delay)
                else:
                    await asyncio.sleep(0)
                wake.clear()
                try:
                    await self._tick(account, backend)
                    delay = 300
                except CursorExpired as e:
                    await self._full_resync(account, backend, e.calendar_id)
                    delay = 5
                except AuthExpired as e:
                    self.auth_expired.emit(account.id, str(e))
                    return
                except TransientError as e:
                    delay = _next_backoff(delay)
                    self.sync_failed.emit(account.id, str(e))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("sync tick crashed for %s", account.id)
                    delay = 300
                    self.sync_failed.emit(account.id, str(e))
        finally:
            self._tasks.pop(account.id, None)
            self._wake_events.pop(account.id, None)

    async def _apply_pending_op(self, backend, op) -> None:
        # Pending ops store the internal DB calendar_id. Backends need the
        # provider_id (CalDAV URL, Google calendar ID, Graph calendar ID).
        cal = await asyncio.to_thread(self._store.get_calendar, op.calendar_id)
        provider_cal_id = cal.provider_id if cal else op.calendar_id

        if op.op == "create":
            event = _event_from_payload(op.payload)
            canonical = await backend.create_event(provider_cal_id, event)
            await asyncio.to_thread(
                self._store.mark_synced,
                event.uid,
                op.calendar_id,
                canonical_uid=canonical.uid,
                provider_event_id=canonical.provider_event_id,
                etag=canonical.etag,
                sequence=canonical.sequence if canonical.sequence else 0,
            )
        elif op.op == "update":
            import dataclasses as _dc

            event = _event_from_payload(op.payload)
            row = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
            if row is None:
                op = await asyncio.to_thread(self._store.get_pending_op, op.id) or op
                row = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
            pid = (row.provider_event_id if row else None) or event.provider_event_id
            if not pid:
                return
            if pid != event.provider_event_id:
                event = _dc.replace(event, provider_event_id=pid)
            canonical = await backend.update_event(
                provider_cal_id, event, if_match=row.etag if row else op.if_match
            )
            if canonical is not None:
                await asyncio.to_thread(
                    self._store.mark_synced,
                    op.uid,
                    op.calendar_id,
                    canonical_uid=canonical.uid,
                    provider_event_id=canonical.provider_event_id,
                    etag=canonical.etag,
                    sequence=canonical.sequence or 0,
                )
        elif op.op == "delete":
            row = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
            if row is None:
                op = await asyncio.to_thread(self._store.get_pending_op, op.id) or op
                row = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
            pid = row.provider_event_id if row else None
            if not pid:
                await asyncio.to_thread(self._store.remove_event, op.uid, op.calendar_id)
                return
            await backend.delete_event(
                provider_cal_id, pid, if_match=row.etag if row else op.if_match
            )
            await asyncio.to_thread(self._store.remove_event, op.uid, op.calendar_id)
        elif op.op == "update_instance":
            event = _event_from_payload(op.payload)
            if event.recurrence_id:
                master = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
                master_pid = master.provider_event_id if master else None
                if master_pid:
                    await backend.update_instance(
                        provider_cal_id, master_pid, event.recurrence_id, event
                    )
                else:
                    log.warning(
                        "update_instance: no provider_event_id for master %s", op.uid
                    )
            else:
                log.warning("update_instance op has no recurrence_id for %s", op.uid)
        elif op.op == "delete_instance":
            import json as _json

            payload = _json.loads(op.payload or "{}")
            rid_str = payload.get("recurrence_id")
            if rid_str:
                from datetime import datetime as _dt

                rid = _dt.fromisoformat(rid_str)
                master = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
                master_pid = master.provider_event_id if master else None
                if master_pid:
                    await backend.delete_instance(provider_cal_id, master_pid, rid)
                else:
                    log.warning(
                        "delete_instance: no provider_event_id for master %s", op.uid
                    )
            else:
                log.warning("delete_instance op missing recurrence_id for %s", op.uid)

    async def _tick(self, account, backend) -> None:
        self.sync_started.emit(account.id)
        n_changes = 0

        # 0) Discover/reconcile calendars (replaces the bootstrap placeholder
        # row with real provider IDs from the backend; picks up new calendars).
        remote_cals = await backend.list_calendars()
        await asyncio.to_thread(self._store.upsert_calendars, account.id, remote_cals)

        # 1) Drain pending writes
        for op in await asyncio.to_thread(self._store.list_pending_ops, account.id):
            try:
                await self._apply_pending_op(backend, op)
                await asyncio.to_thread(self._store.delete_pending_op, op.id)
            except ConflictError:
                log.warning("conflict on %s op for %s; dropping", op.op, op.uid)
                await asyncio.to_thread(self._store.delete_pending_op, op.id)
                self.conflict_detected.emit(op.uid)
            except TransientError:
                raise
            except PermanentError as e:
                # The op will never succeed as-is (bad ID, occurrence delete,
                # etc). Drop it so we don't crash sync every tick — surface the
                # error but keep pulling remote changes.
                log.error("dropping pending %s op for %s: %s", op.op, op.uid, e)
                await asyncio.to_thread(self._store.delete_pending_op, op.id)
                self.sync_failed.emit(account.id, f"{op.op} {op.uid}: {e}")

        # 2) Pull incremental changes per calendar
        for cal in await asyncio.to_thread(self._store.list_calendars, account.id, True):
            from lilical.sync.cursor import cursor_from_json

            cursor = cursor_from_json(
                json.loads(cal.sync_cursor) if cal.sync_cursor else None
            )
            if cursor is None:
                cal_count = 0
                async for changes, new_cur in backend.initial_sync(cal.provider_id):
                    applied = await asyncio.to_thread(
                        self._store.apply_remote_changes,
                        cal.id,
                        changes,
                        json.dumps(new_cur.to_json()),
                    )
                    n_changes += applied
                    cal_count += applied
                    self.sync_progress.emit(account.id, cal.display_name, cal_count)
            else:
                changes, new_cur = await backend.incremental_sync(
                    cal.provider_id, cursor
                )
                applied = await asyncio.to_thread(
                    self._store.apply_remote_changes,
                    cal.id,
                    changes,
                    json.dumps(new_cur.to_json()),
                )
                n_changes += applied
                if applied:
                    self.sync_progress.emit(account.id, cal.display_name, applied)

        self.sync_finished.emit(account.id, n_changes)

    async def _full_resync(self, account, backend, calendar_id: str) -> None:
        log.info("full resync for %s / %s", account.id, calendar_id)
        self.sync_started.emit(account.id)
        # _tick just ran list_calendars (we're here because it raised
        # CursorExpired), so skip the second discovery round-trip and
        # read from the store instead. New calendars surface within
        # one normal tick (≤300s).
        cals = await asyncio.to_thread(self._store.list_calendars, account.id, False)
        for cal in cals:
            if calendar_id and cal.provider_id != calendar_id:
                continue
            cal_count = 0
            async for changes, new_cur in backend.initial_sync(cal.provider_id):
                applied = await asyncio.to_thread(
                    self._store.apply_remote_changes,
                    cal.id,
                    changes,
                    json.dumps(new_cur.to_json()),
                )
                cal_count += applied
                self.sync_progress.emit(account.id, cal.display_name, cal_count)


def _event_from_payload(payload: str | None):
    import json
    from datetime import datetime as _dt

    from lilical.models.event import Event

    if not payload:
        return Event(uid="", calendar_id="")
    data = json.loads(payload)
    # Deserialize ISO-string datetime fields back to datetime objects.
    for field in ("dtstart", "dtend", "recurrence_id", "last_modified"):
        raw = data.get(field)
        if isinstance(raw, str) and raw:
            try:
                data[field] = _dt.fromisoformat(raw)
            except ValueError:
                data[field] = None
    # Deserialize datetime tuple fields.
    for field in ("exdates", "rdates"):
        raw = data.get(field)
        if isinstance(raw, list):
            parsed = []
            for x in raw:
                if isinstance(x, str):
                    with contextlib.suppress(ValueError):
                        parsed.append(_dt.fromisoformat(x))
                elif isinstance(x, _dt):
                    parsed.append(x)
            data[field] = tuple(parsed)
    # Ensure other tuple fields are tuples, not lists.
    for field in ("attendees", "categories", "valarms"):
        if isinstance(data.get(field), list):
            data[field] = tuple(data[field])
    return Event(**{k: v for k, v in data.items() if k in Event.__dataclass_fields__})
