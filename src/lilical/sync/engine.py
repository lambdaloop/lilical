from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random

from PySide6.QtCore import QObject, Signal

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
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
    sync_finished = Signal(str, int)
    sync_failed = Signal(str, str)
    auth_expired = Signal(str)
    conflict_detected = Signal(str)

    def __init__(
        self, store: EventStore, secrets: SecretsStore, factory
    ) -> None:
        super().__init__()
        self._store = store
        self._secrets = secrets
        self._factory = factory
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake_events: dict[str, asyncio.Event] = {}

    async def start_all(self) -> None:
        for acc in self._store.list_accounts(enabled_only=True):
            self._tasks[acc.id] = asyncio.create_task(self._run_account(acc))

    async def stop_all(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def force_refresh(self, account_id: str) -> None:
        self._wake_events[account_id].set()

    async def _run_account(self, account) -> None:
        backend = self._factory(account)
        wake = self._wake_events[account.id] = asyncio.Event()
        delay = 0
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=delay or 1e-9)
            wake.clear()
            try:
                await self._tick(account, backend)
                delay = 300
            except CursorExpired as e:
                await self._full_resync(account, backend, e.calendar_id)
                delay = 5
            except AuthExpired:
                self.auth_expired.emit(account.id)
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

    async def _apply_pending_op(
        self, backend, op
    ) -> None:
        event = _event_from_payload(op.payload)
        if op.op == "create":
            canonical = await backend.create_event(op.calendar_id, event)
            self._store.queue_update(canonical, prev_etag=None)
        elif op.op == "update":
            await backend.update_event(
                op.calendar_id, event, if_match=op.if_match
            )
        elif op.op == "delete":
            await backend.delete_event(
                op.calendar_id, op.uid, if_match=op.if_match
            )

    async def _tick(self, account, backend) -> None:
        self.sync_started.emit(account.id)
        n_changes = 0

        # 1) Drain pending writes
        for op in self._store.list_pending_ops(account.id):
            try:
                await self._apply_pending_op(backend, op)
                self._store.delete_pending_op(op.id)
            except ConflictError:
                self.conflict_detected.emit(op.uid)
            except TransientError:
                raise

        # 2) Pull incremental changes per calendar
        for cal in self._store.list_calendars(account.id, visible_only=False):
            from lilical.sync.cursor import cursor_from_json

            cursor = cursor_from_json(
                json.loads(cal.sync_cursor) if cal.sync_cursor else None
            )
            if cursor is None:
                async for changes, new_cur in backend.initial_sync(cal.provider_id):
                    n_changes += self._store.apply_remote_changes(
                        cal.id,
                        changes,
                        json.dumps(new_cur.to_json()),
                    )
            else:
                changes, new_cur = await backend.incremental_sync(
                    cal.provider_id, cursor
                )
                n_changes += self._store.apply_remote_changes(
                    cal.id,
                    changes,
                    json.dumps(new_cur.to_json()),
                )

        self.sync_finished.emit(account.id, n_changes)

    async def _full_resync(self, account, backend, calendar_id: str) -> None:
        log.info("full resync for %s / %s", account.id, calendar_id)


def _event_from_payload(payload: str | None):
    import json

    from lilical.models.event import Event
    if not payload:
        return Event(uid="", calendar_id="")
    data = json.loads(payload)
    return Event(**{k: v for k, v in data.items() if k in Event.__dataclass_fields__})
