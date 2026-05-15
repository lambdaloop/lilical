from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    EventChange,
    PermanentError,
    TransientError,
)
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
                display_name="cal-1",
            )
        ]

    def apply_remote_changes(self, calendar_id: str, changes: list, new_cursor) -> int:
        self.applied.append((calendar_id, changes, new_cursor))
        return len(changes)

    def upsert_calendars(self, account_id: str, calendars: list) -> None:
        pass


class FakeStoreMultiCal:
    def __init__(self) -> None:
        self.applied: list[tuple[str, list[EventChange], object]] = []

    def list_pending_ops(self, account_id: str) -> list:
        return []

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list:
        return [
            SimpleNamespace(
                id="cal-1",
                provider_id="provider-cal-1",
                sync_cursor=None,
                display_name="cal-1",
            ),
            SimpleNamespace(
                id="cal-2",
                provider_id="provider-cal-2",
                sync_cursor=None,
                display_name="cal-2",
            ),
        ]

    def apply_remote_changes(self, calendar_id: str, changes: list, new_cursor) -> int:
        self.applied.append((calendar_id, changes, new_cursor))
        return len(changes)

    def upsert_calendars(self, account_id: str, calendars: list) -> None:
        pass


class FakeCursor:
    def to_json(self) -> dict:
        return {"type": "fake", "token": "next"}


class FakeBackend:
    def __init__(self) -> None:
        self.initial_sync_calendar_ids: list[str] = []

    async def list_calendars(self) -> list:
        return []

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


@pytest.mark.asyncio
async def test_full_resync_with_specific_calendar_id() -> None:
    """Bug 3 + Bug 4: _full_resync resyncs only the matching calendar."""
    store = FakeStoreMultiCal()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    account = SimpleNamespace(id="acc-1")

    await engine._full_resync(account, backend, "provider-cal-1")

    assert backend.initial_sync_calendar_ids == ["provider-cal-1"]


@pytest.mark.asyncio
async def test_full_resync_with_empty_calendar_id_resyncs_all() -> None:
    """Bug 3: _full_resync with empty calendar_id resyncs all calendars."""
    store = FakeStoreMultiCal()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    account = SimpleNamespace(id="acc-1")

    await engine._full_resync(account, backend, "")

    assert set(backend.initial_sync_calendar_ids) == {
        "provider-cal-1",
        "provider-cal-2",
    }


@pytest.mark.asyncio
async def test_full_resync_applies_remote_changes() -> None:
    """Bug 3: _full_resync calls apply_remote_changes for each calendar."""
    store = FakeStoreMultiCal()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    account = SimpleNamespace(id="acc-1")

    await engine._full_resync(account, backend, "")

    assert len(store.applied) == 2


@pytest.mark.asyncio
async def test_run_account_removes_task_on_auth_expired() -> None:
    """Bug 10: _run_account removes task from _tasks and _wake_events on AuthExpired."""
    engine = SyncEngine(store=MagicMock(), secrets=None, factory=lambda x: MagicMock())
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_pending_ops = MagicMock(return_value=[])

    async def raising_tick(account, backend):
        raise AuthExpired("token expired")

    engine._tick = raising_tick

    sighup = asyncio.Event()

    async def run_and_signal(account):
        await engine._run_account(account)
        sighup.set()

    account = SimpleNamespace(id="acc-expired")
    engine._tasks["acc-expired"] = asyncio.create_task(run_and_signal(account))

    await asyncio.wait_for(sighup.wait(), timeout=5)

    assert "acc-expired" not in engine._tasks
    assert "acc-expired" not in engine._wake_events


@pytest.mark.asyncio
async def test_run_account_removes_task_on_cursor_expired_then_auth_expired() -> None:
    """Bug 10+3: CursorExpired→_full_resync then AuthExpired."""
    engine = SyncEngine(store=MagicMock(), secrets=None, factory=lambda x: MagicMock())
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_pending_ops = MagicMock(return_value=[])
    resync_called = False

    async def tick_with_expiry(account, backend):
        raise CursorExpired("cal-1")

    async def tracking_resync(account, backend, calendar_id):
        nonlocal resync_called
        resync_called = True

    engine._tick = tick_with_expiry
    engine._full_resync = tracking_resync

    sighup = asyncio.Event()

    async def run_and_signal(account):
        await engine._run_account(account)
        sighup.set()

    account = SimpleNamespace(id="acc-cursor")
    engine._tasks["acc-cursor"] = asyncio.create_task(run_and_signal(account))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sighup.wait(), timeout=0.5)

    assert resync_called
    engine._tasks["acc-cursor"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine._tasks["acc-cursor"]


@pytest.mark.asyncio
async def test_start_account_skips_existing_task() -> None:
    """Bug 10: start_account returns early if account_id is already in _tasks."""
    engine = SyncEngine(
        store=MagicMock(),
        secrets=None,
        factory=lambda account: MagicMock(),
    )
    engine._tasks["existing-acc"] = asyncio.create_task(asyncio.sleep(999))

    await engine.start_account("existing-acc")

    assert "existing-acc" in engine._tasks
    engine._tasks["existing-acc"].cancel()


@pytest.mark.asyncio
async def test_start_account_creates_wake_event() -> None:
    """Bug 10: start_account creates a wake_event and adds the task."""
    store = FakeStore()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    engine._store.get_account = MagicMock(return_value=SimpleNamespace(id="new-acc"))
    engine._store.list_pending_ops = MagicMock(return_value=[])
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_accounts = MagicMock(return_value=[])

    await engine.start_account("new-acc")

    assert "new-acc" in engine._tasks
    assert "new-acc" in engine._wake_events
    assert not engine._tasks["new-acc"].done()

    engine._tasks["new-acc"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine._tasks["new-acc"]


@pytest.mark.asyncio
async def test_stop_account_cancels_task_and_clears_registries() -> None:
    store = FakeStore()
    backend = FakeBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda account: backend)
    engine._store.get_account = MagicMock(return_value=SimpleNamespace(id="acc-1"))
    engine._store.list_pending_ops = MagicMock(return_value=[])
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_accounts = MagicMock(return_value=[])

    await engine.start_account("acc-1")
    assert "acc-1" in engine._tasks

    await engine.stop_account("acc-1")

    assert "acc-1" not in engine._tasks
    assert "acc-1" not in engine._wake_events


@pytest.mark.asyncio
async def test_stop_account_noop_for_unknown_account() -> None:
    engine = SyncEngine(
        store=MagicMock(), secrets=None, factory=lambda account: MagicMock()
    )
    # Must not raise even when the account has never been started.
    await engine.stop_account("never-started")


@pytest.mark.asyncio
async def test_force_refresh_sets_wake_event() -> None:
    """Bug 10: force_refresh sets the wake event for an account."""
    engine = SyncEngine(
        store=MagicMock(),
        secrets=None,
        factory=lambda account: MagicMock(),
    )
    ev = asyncio.Event()
    engine._wake_events["test-acc"] = ev

    engine.force_refresh("test-acc")

    assert ev.is_set()


@pytest.mark.asyncio
async def test_force_refresh_restarts_dead_task_after_auth_expired() -> None:
    """force_refresh resurrects an account whose loop terminated on AuthExpired.

    The user's "Sync now" must actually retry after a credentials fix, instead
    of being a silent no-op once the loop has exited.
    """
    engine = SyncEngine(store=MagicMock(), secrets=None, factory=lambda x: MagicMock())
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_pending_ops = MagicMock(return_value=[])

    account = SimpleNamespace(id="acc-resurrect")
    engine._store.get_account = MagicMock(return_value=account)

    # First run: tick raises AuthExpired → loop terminates and pops both dicts.
    async def auth_failing_tick(account, backend):
        raise AuthExpired("401 Unauthorized")

    engine._tick = auth_failing_tick
    sighup_1 = asyncio.Event()

    async def run_and_signal(acc, ev):
        await engine._run_account(acc)
        ev.set()

    engine._tasks["acc-resurrect"] = asyncio.create_task(
        run_and_signal(account, sighup_1)
    )
    await asyncio.wait_for(sighup_1.wait(), timeout=5)
    assert "acc-resurrect" not in engine._tasks
    assert "acc-resurrect" not in engine._wake_events

    # Swap in a success tick before resurrecting, so the second run doesn't loop
    # AuthExpired forever (and so we can confirm a wake_event reappears).
    second_tick_called = asyncio.Event()

    async def succeeding_tick(account, backend):
        second_tick_called.set()

    engine._tick = succeeding_tick

    # force_refresh on a dead account must restart it. The resurrection is
    # async (DB fetch via to_thread), so _tasks is populated only after the
    # first tick fires — confirmed below.
    engine.force_refresh("acc-resurrect")

    # Give the new task one tick of the event loop, then confirm the second
    # _tick fired and a fresh wake_event was registered.
    await asyncio.wait_for(second_tick_called.wait(), timeout=5)
    assert "acc-resurrect" in engine._tasks
    assert "acc-resurrect" in engine._wake_events

    # Clean up the still-running resurrected task.
    task = engine._tasks["acc-resurrect"]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@pytest.mark.asyncio
async def test_force_refresh_noop_when_account_unknown() -> None:
    """force_refresh on an unknown account must not crash or create a task."""
    engine = SyncEngine(store=MagicMock(), secrets=None, factory=lambda x: MagicMock())
    engine._store.get_account = MagicMock(return_value=None)

    engine.force_refresh("ghost-acc")

    assert "ghost-acc" not in engine._tasks
    assert "ghost-acc" not in engine._wake_events


@pytest.mark.asyncio
async def test_tick_with_cursor_expired_propagates_to_run_account() -> None:
    """Bug 3 + Bug 4: CursorExpired raised during tick is caught by _run_account."""
    engine = SyncEngine(store=MagicMock(), secrets=None, factory=lambda x: MagicMock())
    engine._store.list_calendars = MagicMock(return_value=[])
    engine._store.list_pending_ops = MagicMock(return_value=[])
    engine._store.list_accounts = MagicMock(return_value=[])
    resync_calendar_ids = []

    async def tick_with_expiry(account, backend):
        raise CursorExpired("cal-1")

    async def tracking_resync(account, backend, calendar_id):
        resync_calendar_ids.append(calendar_id)

    engine._tick = tick_with_expiry
    engine._full_resync = tracking_resync

    account = SimpleNamespace(id="acc-cursor2")
    engine._tasks[account.id] = asyncio.create_task(engine._run_account(account))
    await asyncio.sleep(0.1)

    assert "cal-1" in resync_calendar_ids

    engine._tasks[account.id].cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine._tasks[account.id]


# -- discovery: _tick must call list_calendars + upsert before draining deltas


class _RecordingStore:
    """Records every call so we can assert ordering."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.calendars: list = []

    def list_pending_ops(self, account_id: str) -> list:
        self.calls.append(f"list_pending_ops({account_id})")
        return []

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list:
        self.calls.append(f"list_calendars({account_id})")
        return self.calendars

    def apply_remote_changes(self, calendar_id, changes, new_cursor) -> int:
        self.calls.append(f"apply({calendar_id},n={len(changes)})")
        return len(changes)

    def upsert_calendars(self, account_id: str, calendars: list) -> None:
        self.calls.append(f"upsert({len(calendars)})")


class _DiscoveryBackend:
    def __init__(self, remote_cals, pages_per_cal=None) -> None:
        self._remote = remote_cals
        self._pages_per_cal = pages_per_cal or {}
        self.list_calendars_call_count = 0

    async def list_calendars(self) -> list:
        self.list_calendars_call_count += 1
        return self._remote

    async def initial_sync(self, calendar_id: str):
        pages = self._pages_per_cal.get(calendar_id, [])
        for batch in pages:
            yield batch, FakeCursor()


@pytest.mark.asyncio
async def test_tick_calls_list_calendars_and_upsert_before_pulling_deltas() -> None:
    """Bug: without discovery, the engine asked Graph for /me/calendars/default
    (a stub provider_id) and got 400. Order must be:
      list_calendars → upsert → list local calendars → initial_sync."""
    store = _RecordingStore()
    store.calendars = []  # nothing to drain
    backend = _DiscoveryBackend(
        remote_cals=[{"provider_id": "real", "display_name": "Real"}]
    )
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert backend.list_calendars_call_count == 1
    assert store.calls[0].startswith("upsert(")
    # list_calendars-on-store must come AFTER upsert so the placeholder is gone.
    upsert_idx = next(i for i, c in enumerate(store.calls) if c.startswith("upsert"))
    list_idx = next(
        i for i, c in enumerate(store.calls) if c.startswith("list_calendars")
    )
    assert upsert_idx < list_idx


# -- sync_progress: must emit per-page during initial_sync


class _MultiPageBackend:
    def __init__(self, pages: list[list[EventChange]]) -> None:
        self._pages = pages

    async def list_calendars(self) -> list:
        return []

    async def initial_sync(self, calendar_id: str):
        for batch in self._pages:
            yield batch, FakeCursor()


@pytest.mark.asyncio
async def test_tick_emits_sync_progress_per_page() -> None:
    store = FakeStore()
    pages = [
        [
            EventChange(
                kind="upsert",
                event=Event(uid=f"e{i}", calendar_id="cal-1"),
                uid=f"e{i}",
            )
            for i in range(3)
        ],
        [
            EventChange(
                kind="upsert",
                event=Event(uid=f"e{i}", calendar_id="cal-1"),
                uid=f"e{i}",
            )
            for i in range(3, 5)
        ],
    ]
    backend = _MultiPageBackend(pages)
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    progress: list[tuple[str, str, int]] = []
    engine.sync_progress.connect(
        lambda account_id, label, count: progress.append((account_id, label, count))
    )

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(progress) == 2
    assert progress[0][0] == "acc-1"
    assert progress[0][1] == "cal-1"  # display_name on the seeded FakeStore row
    assert progress[0][2] == 3  # first page count
    assert progress[1][2] == 5  # running total after second page


# -- _full_resync: discovery + progress as well


@pytest.mark.asyncio
async def test_full_resync_runs_discovery_and_emits_progress() -> None:
    store = FakeStoreMultiCal()
    pages_by_cal = {
        "provider-cal-1": [
            [
                EventChange(
                    kind="upsert", event=Event(uid="x", calendar_id="cal-1"), uid="x"
                )
            ],
        ],
        "provider-cal-2": [
            [
                EventChange(
                    kind="upsert", event=Event(uid="y", calendar_id="cal-2"), uid="y"
                )
            ],
            [
                EventChange(
                    kind="upsert", event=Event(uid="z", calendar_id="cal-2"), uid="z"
                )
            ],
        ],
    }
    backend = _DiscoveryBackend(remote_cals=[], pages_per_cal=pages_by_cal)
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    progress: list[tuple[str, str, int]] = []
    engine.sync_progress.connect(
        lambda account_id, label, count: progress.append((account_id, label, count))
    )

    await engine._full_resync(SimpleNamespace(id="acc-1"), backend, "")

    assert (
        backend.list_calendars_call_count == 0
    )  # _full_resync reads from store, not backend
    counts_per_cal = {(label, count) for _, label, count in progress}
    assert ("cal-1", 1) in counts_per_cal
    assert ("cal-2", 1) in counts_per_cal
    assert ("cal-2", 2) in counts_per_cal  # cumulative after second page


# -- sync_failed event drains the progress-bar state in main_window (engine-side fanout)


@pytest.mark.asyncio
async def test_run_account_emits_sync_failed_on_transient_error() -> None:
    from lilical.backends.base import TransientError

    class _FailingBackend:
        async def list_calendars(self) -> list:
            raise TransientError("server down")

        async def initial_sync(self, _):
            yield  # pragma: no cover (never reached)

    store = FakeStore()
    engine = SyncEngine(store, secrets=None, factory=lambda a: _FailingBackend())

    failed: list[tuple[str, str]] = []
    engine.sync_failed.connect(lambda acc, msg: failed.append((acc, msg)))

    with pytest.raises(TransientError):
        await engine._tick(SimpleNamespace(id="acc-1"), _FailingBackend())

    # sync_failed is emitted by _run_account, not _tick. Drive _run_account
    # for one iteration via the wake event then cancel.
    engine._wake_events["acc-1"] = asyncio.Event()
    task = asyncio.create_task(engine._run_account(SimpleNamespace(id="acc-1")))
    # Give it a tick to run.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(acc == "acc-1" and "server down" in msg for acc, msg in failed)


# ── pending-op drain pipeline ─────────────────────────────────────────────────


def _op(op_type: str, uid: str = "uid-1", calendar_id: str = "cal-1", if_match=None):
    import json

    payload = json.dumps({"uid": uid, "calendar_id": calendar_id})
    return SimpleNamespace(
        id=1,
        op=op_type,
        uid=uid,
        calendar_id=calendar_id,
        payload=payload,
        if_match=if_match,
    )


class _PendingOpStore:
    """FakeStore variant that tracks pending-op mutations."""

    def __init__(self, ops: list) -> None:
        self._ops = ops
        self.deleted_op_ids: list[int] = []
        self.queue_update_calls: list[tuple] = []
        self.mark_synced_calls: list[dict] = []

    def list_pending_ops(self, account_id: str) -> list:
        return list(self._ops)

    def list_calendars(self, account_id: str, visible_only: bool = True) -> list:
        return []

    def apply_remote_changes(self, calendar_id, changes, new_cursor) -> int:
        return 0

    def upsert_calendars(self, account_id: str, calendars: list) -> None:
        pass

    def delete_pending_op(self, op_id: int) -> None:
        self.deleted_op_ids.append(op_id)

    def queue_update(self, event, prev_etag) -> None:
        self.queue_update_calls.append((event, prev_etag))

    def get_calendar(self, calendar_id: str):
        from types import SimpleNamespace

        return SimpleNamespace(provider_id=calendar_id)

    def get_event(self, uid: str, calendar_id: str):
        from types import SimpleNamespace

        # Return a minimal row so delete ops (which now require
        # provider_event_id) reach the backend. Tests that need to
        # simulate "never synced" override this per-instance.
        return SimpleNamespace(provider_event_id=uid, etag='"mock-etag"')

    def get_pending_op(self, op_id: int):
        return None

    def remove_event(self, uid: str, calendar_id: str) -> None:
        pass

    def mark_synced(
        self,
        local_uid: str,
        calendar_id: str,
        *,
        canonical_uid,
        provider_event_id,
        etag,
        sequence,
    ) -> None:
        self.mark_synced_calls.append(
            {
                "uid": local_uid,
                "calendar_id": calendar_id,
                "canonical_uid": canonical_uid,
                "provider_event_id": provider_event_id,
                "etag": etag,
                "sequence": sequence,
            }
        )


class _WriteBackend:
    """Backend that records write calls and can be configured to raise."""

    def __init__(
        self,
        create_raises=None,
        update_raises=None,
        delete_raises=None,
        canonical_event=None,
    ) -> None:
        self.creates: list = []
        self.updates: list = []
        self.deletes: list = []
        self._create_raises = create_raises
        self._update_raises = update_raises
        self._delete_raises = delete_raises
        self._canonical = canonical_event

    async def list_calendars(self) -> list:
        return []

    async def create_event(self, calendar_id: str, event) -> object:
        self.creates.append((calendar_id, event))
        if self._create_raises:
            raise self._create_raises
        return self._canonical or event

    async def update_event(self, calendar_id: str, event, if_match=None) -> object:
        self.updates.append((calendar_id, event, if_match))
        if self._update_raises:
            raise self._update_raises
        return self._canonical or event

    async def delete_event(self, calendar_id: str, uid: str, if_match=None) -> None:
        self.deletes.append((calendar_id, uid, if_match))
        if self._delete_raises:
            raise self._delete_raises


@pytest.mark.asyncio
async def test_tick_drains_pending_ops_in_fifo_order() -> None:
    ops = [
        _op("create", uid="u1"),
        _op("update", uid="u2"),
        _op("delete", uid="u3"),
    ]
    for i, op in enumerate(ops):
        op.id = i + 1
    store = _PendingOpStore(ops)
    backend = _WriteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(backend.creates) == 1
    assert len(backend.updates) == 1
    assert len(backend.deletes) == 1
    assert store.deleted_op_ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_tick_deletes_pending_op_on_success() -> None:
    op = _op("delete", uid="u1")
    op.id = 99
    store = _PendingOpStore([op])
    backend = _WriteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert 99 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_conflict_error_emits_conflict_detected_and_drops_op() -> None:
    op = _op("update", uid="u-conflict")
    op.id = 77
    store = _PendingOpStore([op])
    backend = _WriteBackend(update_raises=ConflictError("412"))
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    conflicts: list[str] = []
    engine.conflict_detected.connect(lambda uid: conflicts.append(uid))

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert "u-conflict" in conflicts
    assert 77 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_permanent_error_drops_pending_op_and_emits_sync_failed() -> None:
    op = _op("delete", uid="u-perm")
    op.id = 55
    store = _PendingOpStore([op])
    backend = _WriteBackend(delete_raises=PermanentError("404 not found"))
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    failures: list[tuple[str, str]] = []
    engine.sync_failed.connect(lambda acc, msg: failures.append((acc, msg)))

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert 55 in store.deleted_op_ids
    assert any(acc == "acc-1" for acc, _ in failures)


@pytest.mark.asyncio
async def test_transient_error_during_drain_propagates() -> None:
    op = _op("update", uid="u-transient")
    op.id = 33
    store = _PendingOpStore([op])
    backend = _WriteBackend(update_raises=TransientError("503"))
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    with pytest.raises(TransientError):
        await engine._tick(SimpleNamespace(id="acc-1"), backend)

    # Op must NOT be deleted when a TransientError propagates
    assert 33 not in store.deleted_op_ids


@pytest.mark.asyncio
async def test_apply_pending_create_marks_synced_with_canonical() -> None:
    """After a successful create, engine calls mark_synced (not queue_update)."""
    canonical = Event(
        uid="u-new",
        calendar_id="cal-1",
        summary="Canonical",
        provider_event_id="server-id-123",
        etag='"abc"',
    )
    op = _op("create", uid="u-new")
    op.id = 11
    store = _PendingOpStore([op])
    backend = _WriteBackend(canonical_event=canonical)
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    # No re-queue via queue_update
    assert len(store.queue_update_calls) == 0
    # mark_synced called once with provider_event_id from canonical
    assert len(store.mark_synced_calls) == 1
    call = store.mark_synced_calls[0]
    assert call["uid"] == "u-new"
    assert call["canonical_uid"] == "u-new"
    assert call["provider_event_id"] == "server-id-123"
    assert call["etag"] == '"abc"'


# ── _event_from_payload edge cases ────────────────────────────────────────────


def test_event_from_payload_none_returns_empty_event() -> None:
    from lilical.sync.engine import _event_from_payload

    event = _event_from_payload(None)
    assert event.uid == ""
    assert event.calendar_id == ""


def test_event_from_payload_empty_string_returns_empty_event() -> None:
    from lilical.sync.engine import _event_from_payload

    event = _event_from_payload("")
    assert event.uid == ""


def test_event_from_payload_extracts_known_fields() -> None:
    import json

    from lilical.sync.engine import _event_from_payload

    payload = json.dumps({"uid": "u1", "calendar_id": "cal-1", "summary": "Test"})
    event = _event_from_payload(payload)
    assert event.uid == "u1"
    assert event.summary == "Test"


def test_event_from_payload_ignores_unknown_fields() -> None:
    import json

    from lilical.sync.engine import _event_from_payload

    payload = json.dumps(
        {"uid": "u2", "calendar_id": "cal-1", "unknown_field": "should_be_ignored"}
    )
    event = _event_from_payload(payload)
    assert event.uid == "u2"


# ── recurring: update_instance / delete_instance dispatch ─────────────────────


@pytest.mark.asyncio
async def test_tick_dispatches_update_instance_op_to_backend() -> None:
    """update_instance ops must call backend.update_instance with the master's
    provider_event_id and the parsed recurrence_id datetime."""
    import json
    from datetime import datetime, timezone

    recurrence_id = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    master_event = Event(
        uid="uid-master",
        calendar_id="cal-1",
        provider_event_id="AAMk-master",
        summary="Standup",
    )

    class _InstanceWriteBackend(_WriteBackend):
        def __init__(self):
            super().__init__()
            self.instance_updates: list = []

        async def update_instance(self, calendar_id, master_pid, rid, event):
            self.instance_updates.append((calendar_id, master_pid, rid, event))

    class _InstanceOpStore(_PendingOpStore):
        def get_event(self, uid, calendar_id):
            return master_event if uid == "uid-master" else None

    payload = json.dumps(
        {
            "uid": "uid-master",
            "calendar_id": "cal-1",
            "recurrence_id": recurrence_id.isoformat(),
            "summary": "Standup (moved)",
        }
    )
    op = SimpleNamespace(
        id=1,
        op="update_instance",
        uid="uid-master",
        calendar_id="cal-1",
        payload=payload,
        if_match=None,
    )
    store = _InstanceOpStore([op])
    backend = _InstanceWriteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(backend.instance_updates) == 1
    cal_id, mpid, rid, ev = backend.instance_updates[0]
    assert cal_id == "cal-1"
    assert mpid == "AAMk-master"
    assert rid == recurrence_id
    assert 1 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_tick_dispatches_delete_instance_op_to_backend() -> None:
    """delete_instance ops must call backend.delete_instance with the master's
    provider_event_id and the parsed recurrence_id datetime."""
    import json
    from datetime import datetime, timezone

    recurrence_id = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    master_event = Event(
        uid="uid-master",
        calendar_id="cal-1",
        provider_event_id="AAMk-master",
        summary="Standup",
    )

    class _InstanceDeleteBackend(_WriteBackend):
        def __init__(self):
            super().__init__()
            self.instance_deletes: list = []

        async def delete_instance(self, calendar_id, master_pid, rid):
            self.instance_deletes.append((calendar_id, master_pid, rid))

    class _InstanceOpStore(_PendingOpStore):
        def get_event(self, uid, calendar_id):
            return master_event if uid == "uid-master" else None

    payload = json.dumps({"recurrence_id": recurrence_id.isoformat()})
    op = SimpleNamespace(
        id=2,
        op="delete_instance",
        uid="uid-master",
        calendar_id="cal-1",
        payload=payload,
        if_match=None,
    )
    store = _InstanceOpStore([op])
    backend = _InstanceDeleteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(backend.instance_deletes) == 1
    cal_id, mpid, rid = backend.instance_deletes[0]
    assert cal_id == "cal-1"
    assert mpid == "AAMk-master"
    assert rid == recurrence_id
    assert 2 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_delete_op_resolves_provider_event_id_from_store() -> None:
    """Delete op must pass the event's provider_event_id to backend.delete_event,
    not raw op.uid (which is the iCalUID — wrong for Google/Graph deletes)."""
    from lilical.models.event import Event

    stored_event = Event(
        uid="u-del",
        calendar_id="cal-1",
        provider_event_id="server-id-del",
    )

    class _IdResolvingStore(_PendingOpStore):
        def get_event(self, uid, calendar_id):
            return stored_event if uid == "u-del" else None

    op = _op("delete", uid="u-del")
    op.id = 42
    store = _IdResolvingStore([op])
    backend = _WriteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(backend.deletes) == 1
    _, pid, _ = backend.deletes[0]
    assert pid == "server-id-del", f"Expected provider_event_id, got {pid!r}"
    assert 42 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_delete_op_skipped_when_no_provider_event_id() -> None:
    """Engine skips backend.delete_event when the row has no provider_event_id."""
    op = _op("delete", uid="u-unsynced")
    op.id = 77
    store = _PendingOpStore([op])
    # Override get_event to return a row with no provider_event_id
    store.get_event = lambda uid, cal: SimpleNamespace(provider_event_id=None)  # type: ignore[method-assign]
    backend = _WriteBackend()
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    assert len(backend.deletes) == 0
    assert 77 in store.deleted_op_ids


@pytest.mark.asyncio
async def test_update_op_refreshes_etag() -> None:
    """Engine calls mark_synced after a successful update, refreshing the etag."""
    import json

    canonical = Event(
        uid="u-upd",
        calendar_id="cal-1",
        summary="Updated",
        provider_event_id="pid-u-upd",
        etag='"new-etag"',
    )
    payload = json.dumps({"uid": "u-upd", "calendar_id": "cal-1", "summary": "Updated"})
    op = SimpleNamespace(
        id=22,
        op="update",
        uid="u-upd",
        calendar_id="cal-1",
        payload=payload,
        if_match='"old-etag"',
    )
    store = _PendingOpStore([op])
    backend = _WriteBackend(canonical_event=canonical)
    engine = SyncEngine(store, secrets=None, factory=lambda a: backend)

    await engine._tick(SimpleNamespace(id="acc-1"), backend)

    # mark_synced was called with the new etag from the canonical
    assert len(store.mark_synced_calls) >= 1
    last_call = store.mark_synced_calls[-1]
    assert last_call["etag"] == '"new-etag"'
    assert last_call["uid"] == "u-upd"
    assert last_call["canonical_uid"] == "u-upd"
    assert last_call["provider_event_id"] == "pid-u-upd"
