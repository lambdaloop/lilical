from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lilical.backends.base import EventChange
from lilical.models.event import Event
from lilical.sync.engine import SyncEngine


class FakeStore:
    def __init__(self) -> None:
        self.applied: list[tuple[str, list[EventChange], object]] = []
        self.visible_only_args: list[bool] = []

    def list_pending_ops(self, account_id: str) -> list:
        assert account_id == "acc-1"
        return []

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list:
        assert account_id == "acc-1"
        self.visible_only_args.append(visible_only)
        return [
            SimpleNamespace(
                id="cal-1",
                provider_id="provider-cal-1",
                sync_cursor=None,
            )
        ]

    def apply_remote_changes(self, calendar_id: str, changes: list, new_cursor) -> int:
        self.applied.append((calendar_id, changes, new_cursor))
        return len(changes)


class FakeCursor:
    def to_json(self) -> dict:
        return {"type": "fake", "token": "next"}


class FakeBackend:
    def __init__(self) -> None:
        self.initial_sync_calendar_ids: list[str] = []

    async def initial_sync(self, calendar_id: str):
        self.initial_sync_calendar_ids.append(calendar_id)
        event = Event(uid="event-1", calendar_id="cal-1", summary="Remote event")
        yield [EventChange(kind="upsert", event=event, uid="event-1")], FakeCursor()


@pytest.mark.asyncio
async def test_tick_runs_initial_sync_and_applies_remote_changes() -> None:
    store = FakeStore()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    finished: list[tuple[str, int]] = []
    engine.sync_finished.connect(
        lambda account_id, count: finished.append((account_id, count))
    )

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert store.visible_only_args == [False]
    assert backend.initial_sync_calendar_ids == ["provider-cal-1"]
    assert len(store.applied) == 1
    calendar_id, changes, cursor = store.applied[0]
    assert calendar_id == "cal-1"
    assert len(changes) == 1
    assert json.loads(cursor) == {"type": "fake", "token": "next"}
    assert finished == [("acc-1", 1)]
