from __future__ import annotations

import asyncio
import json
import logging
import random

from PySide6.QtCore import QObject, Signal

from lilical.backends.base import (
    AuthExpired,
    Backend,
    ConflictError,
    CursorExpired,
    SyncCursor,
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
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay or 1e-9)
            except asyncio.TimeoutError:
                pass
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

    async def _tick(self, account, backend) -> None:
        self.sync_started.emit(account.id)
        n_changes = 0
        for cal in self._store.list_calendars(account.id):
            self.sync_finished.emit(account.id, n_changes)

    async def _full_resync(self, account, backend, calendar_id: str) -> None:
        log.info("full resync for %s / %s", account.id, calendar_id)
