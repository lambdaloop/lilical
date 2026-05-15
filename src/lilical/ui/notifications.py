from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from lilical.recurrence.expander import RecurrenceExpander
from lilical.storage.event_store import EventStore


class NotificationScheduler:
    def __init__(self, store: EventStore, recurrence: RecurrenceExpander) -> None:
        self._store = store
        self._recurrence = recurrence
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        pass
