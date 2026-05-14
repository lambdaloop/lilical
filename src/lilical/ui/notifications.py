from __future__ import annotations

import asyncio

from lilical.storage.event_store import EventStore
from lilical.recurrence.expander import RecurrenceExpander


class NotificationScheduler:
    def __init__(self, store: EventStore, recurrence: RecurrenceExpander) -> None:
        self._store = store
        self._recurrence = recurrence
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        pass
