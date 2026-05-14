from __future__ import annotations

from lilical.storage.event_store import EventStore


class RecurrenceExpander:
    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._cache: dict = {}
