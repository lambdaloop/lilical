from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any, cast

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


_CAL_CONCURRENCY = 4  # max parallel calendar syncs per account


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
    contacts_sync_started = Signal(str)  # account_id
    contacts_sync_finished = Signal(str, int)  # account_id, count

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

    def force_full_resync(self, account_id: str) -> None:
        """Clear sync cursors for this account and wake the loop for a full resync."""
        self._store.reset_sync_cursors(account_id)
        self.force_refresh(account_id)

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
        wake = self._wake_events.setdefault(account.id, asyncio.Event())
        delay = 2  # small grace period before first sync so UI settles
        try:
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                wake.clear()
                try:
                    await self._tick(account, backend)
                    # Yield so UI coroutines can process between ticks.
                    await asyncio.sleep(0)
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
                row = await asyncio.to_thread(
                    self._store.get_event, op.uid, op.calendar_id
                )
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
                row = await asyncio.to_thread(
                    self._store.get_event, op.uid, op.calendar_id
                )
            pid = row.provider_event_id if row else None
            if not pid:
                await asyncio.to_thread(
                    self._store.remove_event, op.uid, op.calendar_id
                )
                return
            await backend.delete_event(
                provider_cal_id, pid, if_match=row.etag if row else op.if_match
            )
            await asyncio.to_thread(self._store.remove_event, op.uid, op.calendar_id)
        elif op.op == "update_instance":
            event = _event_from_payload(op.payload)
            if event.recurrence_id:
                master = await asyncio.to_thread(
                    self._store.get_event, op.uid, op.calendar_id
                )
                master_pid = master.provider_event_id if master else None
                if master_pid:
                    override_etag = await asyncio.to_thread(
                        self._store.get_override_etag,
                        op.uid,
                        op.calendar_id,
                        event.recurrence_id,
                        bool(master and master.all_day),
                    )
                    await backend.update_instance(
                        provider_cal_id,
                        master_pid,
                        event.recurrence_id,
                        event,
                        if_match=override_etag or event.etag,
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
                master = await asyncio.to_thread(
                    self._store.get_event, op.uid, op.calendar_id
                )
                master_pid = master.provider_event_id if master else None
                if master_pid:
                    override_etag = await asyncio.to_thread(
                        self._store.get_override_etag,
                        op.uid,
                        op.calendar_id,
                        rid,
                        bool(master and master.all_day),
                    )
                    await backend.delete_instance(
                        provider_cal_id,
                        master_pid,
                        rid,
                        if_match=override_etag,
                        all_day=bool(master and master.all_day),
                    )
                    # The tombstone may now release itself if the server ever
                    # reports this occurrence as present again.
                    await asyncio.to_thread(
                        self._store.mark_delete_instance_pushed,
                        op.uid,
                        op.calendar_id,
                        rid,
                    )
                else:
                    # Do not swallow this: falling through would reap the op as
                    # though it had succeeded, silently losing the deletion.
                    raise PermanentError(
                        f"delete_instance: no provider_event_id for master {op.uid}"
                    )
            else:
                raise PermanentError(
                    f"delete_instance op missing recurrence_id for {op.uid}"
                )
        elif op.op == "respond":
            import json as _json

            payload = _json.loads(op.payload or "{}")
            response = payload.get("response", "")
            if not response:
                return
            row = await asyncio.to_thread(self._store.get_event, op.uid, op.calendar_id)
            if row is None:
                return
            canonical = await backend.respond_to_event(provider_cal_id, row, response)
            if canonical is not None:
                await asyncio.to_thread(
                    self._store.mark_synced,
                    op.uid,
                    op.calendar_id,
                    canonical_uid=canonical.uid,
                    provider_event_id=canonical.provider_event_id,
                    etag=canonical.etag,
                    sequence=canonical.sequence or row.sequence or 0,
                )

    async def _initial_sync_cal(self, account_id: str, cal, backend) -> int:
        """Initial-sync one calendar, pre-fetching the next page during each DB write."""  # noqa: E501
        _done = object()

        async def _next(gen):
            try:
                return await gen.__anext__()
            except StopAsyncIteration:
                return _done

        gen = backend.initial_sync(cal.provider_id)
        fetch = asyncio.create_task(_next(gen))
        cal_count = 0
        try:
            while True:
                result = await fetch
                if result is _done:
                    break
                changes, new_cur = cast("tuple[list, Any]", result)
                fetch = asyncio.create_task(_next(gen))
                applied = await asyncio.to_thread(
                    self._store.apply_remote_changes,
                    cal.id,
                    changes,
                    json.dumps(new_cur.to_json()),
                    rebuild_batch_size=0,
                )
                cal_count += applied
                self.sync_progress.emit(account_id, cal.display_name, cal_count)
                # Yield so the event loop can process UI tasks between pages.
                await asyncio.sleep(0)
        except BaseException:
            fetch.cancel()
            with contextlib.suppress(BaseException):
                await fetch
            raise
        return cal_count

    async def _tick(self, account, backend) -> None:
        self.sync_started.emit(account.id)
        n_changes = 0

        # 0) Discover/reconcile calendars (replaces the bootstrap placeholder
        # row with real provider IDs from the backend; picks up new calendars).
        remote_cals = await backend.list_calendars()
        await asyncio.to_thread(self._store.upsert_calendars, account.id, remote_cals)

        # 1) Drain pending writes.
        # Successful ops are reaped only after the pull below, not here: an op
        # is the last thing protecting a local change from being overwritten by
        # the server state pulled in this same tick. Reaping late costs nothing
        # (the op is not re-sent) and closes that window.
        reap_after_pull: list[int] = []
        for op in await asyncio.to_thread(self._store.list_pending_ops, account.id):
            try:
                await self._apply_pending_op(backend, op)
                reap_after_pull.append(op.id)
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

        # 2) Pull incremental changes per calendar (parallel, bounded to _CAL_CONCURRENCY)  # noqa: E501
        from lilical.sync.cursor import cursor_from_json

        cals = await asyncio.to_thread(self._store.list_calendars, account.id, True)
        sem = asyncio.Semaphore(_CAL_CONCURRENCY)

        async def _sync_one(cal) -> int:
            async with sem:
                cursor = cursor_from_json(
                    json.loads(cal.sync_cursor) if cal.sync_cursor else None
                )
                if cursor is None:
                    return await self._initial_sync_cal(account.id, cal, backend)
                changes, new_cur = await backend.incremental_sync(
                    cal.provider_id, cursor
                )
                applied = await asyncio.to_thread(
                    self._store.apply_remote_changes,
                    cal.id,
                    changes,
                    json.dumps(new_cur.to_json()),
                )
                if applied:
                    self.sync_progress.emit(account.id, cal.display_name, applied)
                return applied

        results = await asyncio.gather(
            *[_sync_one(c) for c in cals], return_exceptions=True
        )
        for r in results:
            if isinstance(r, int):
                n_changes += r
            else:
                # Keep the ops: the pull failed, so nothing has overwritten the
                # local state and a retry is harmless.
                raise r

        # The pull is done and local state survived it — now the ops can go.
        for op_id in reap_after_pull:
            await asyncio.to_thread(self._store.delete_pending_op, op_id)

        # 3) Sync contacts (lower-frequency — once per 24 h per source).
        contact_store = getattr(self._store, "contacts", None)
        if contact_store is not None:
            for source in backend.supported_contact_sources():
                if await asyncio.to_thread(
                    contact_store.needs_refresh, account.id, source
                ):
                    await self._sync_contacts(account, backend, source, contact_store)

        # 4) Harvest contacts from already-synced event attendees/organizers.
        if contact_store is not None:
            await self._harvest_contacts(account.id, contact_store)

        self.sync_finished.emit(account.id, n_changes)

    async def _sync_contacts(
        self, account, backend, source: str, contact_store
    ) -> None:
        self.contacts_sync_started.emit(account.id)
        count = 0
        cursor = None
        state = await asyncio.to_thread(
            contact_store.get_sync_state, account.id, source
        )
        if state is not None:
            import json as _json

            try:
                cursor = _json.loads(state.cursor) if state.cursor else None
            except Exception:
                cursor = None
        try:
            while True:
                contacts, next_cursor, is_done = await backend.list_contacts(
                    source, cursor
                )
                if contacts:
                    await asyncio.to_thread(
                        contact_store.upsert_many, account.id, source, contacts
                    )
                    count += len(contacts)
                import json as _json

                await asyncio.to_thread(
                    contact_store.set_sync_state,
                    account.id,
                    source,
                    _json.dumps(next_cursor) if next_cursor else None,
                    mark_refreshed=is_done,
                )
                cursor = next_cursor
                if is_done:
                    break
        except Exception as e:
            log.warning("contacts sync failed for %s/%s: %s", account.id, source, e)
        self.contacts_sync_finished.emit(account.id, count)

    async def _harvest_contacts(self, account_id: str, contact_store) -> None:

        cals = await asyncio.to_thread(self._store.list_calendars, account_id, False)
        if not cals:
            return
        pairs = await asyncio.to_thread(self._gather_harvest_pairs, cals)
        if pairs:
            await asyncio.to_thread(contact_store.upsert_harvested, account_id, pairs)

    def _gather_harvest_pairs(self, cals) -> list[tuple[str, str | None]]:
        """Synchronous — runs in a thread to avoid blocking the event loop."""
        import json as _j

        from sqlalchemy.orm import Session

        from lilical.models.event import EventRow

        pairs: list[tuple[str, str | None]] = []
        for cal in cals:
            with Session(self._store._engine) as s:
                rows = (
                    s.query(EventRow)
                    .filter(EventRow.calendar_id == cal.id, EventRow.attendees != None)  # noqa: E711
                    .all()
                )
            for row in rows:
                if row.attendees:
                    try:
                        for a in _j.loads(row.attendees):
                            if isinstance(a, dict) and a.get("email"):
                                pairs.append((a["email"], a.get("display_name")))
                    except Exception:
                        pass
                if row.organizer:
                    try:
                        o = _j.loads(row.organizer)
                        if isinstance(o, dict) and o.get("email"):
                            pairs.append((o["email"], o.get("display_name")))
                    except Exception:
                        pass
        return pairs

    async def _full_resync(self, account, backend, calendar_id: str) -> None:
        log.info("full resync for %s / %s", account.id, calendar_id)
        self.sync_started.emit(account.id)
        # _tick just ran list_calendars (we're here because it raised
        # CursorExpired), so skip the second discovery round-trip and
        # read from the store instead. New calendars surface within
        # one normal tick (≤300s).
        cals = await asyncio.to_thread(self._store.list_calendars, account.id, True)
        for cal in cals:
            if calendar_id and cal.provider_id != calendar_id:
                continue
            await self._initial_sync_cal(account.id, cal, backend)


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
    # Deserialize attendees list of dicts → tuple of Attendee.
    from lilical.models.event import Attendee, Organizer

    raw_attendees = data.get("attendees")
    if isinstance(raw_attendees, list):
        rebuilt: list[Attendee] = []
        for a in raw_attendees:
            if isinstance(a, dict):
                rebuilt.append(
                    Attendee(
                        email=str(a.get("email", "")),
                        display_name=a.get("display_name") or None,
                        response=str(a.get("response", "NEEDS-ACTION")),
                        is_organizer=bool(a.get("is_organizer", False)),
                        is_self=bool(a.get("is_self", False)),
                    )
                )
            elif isinstance(a, str):
                # legacy string shape
                email = a.rsplit(":", 1)[-1] if ":" in a else a
                if email.lower().startswith("mailto:"):
                    email = email[7:]
                rebuilt.append(Attendee(email=email))
        data["attendees"] = tuple(rebuilt)

    # Deserialize organizer dict → Organizer.
    raw_org = data.get("organizer")
    if isinstance(raw_org, dict):
        data["organizer"] = Organizer(
            email=str(raw_org.get("email", "")),
            display_name=raw_org.get("display_name") or None,
            is_self=bool(raw_org.get("is_self", False)),
        )
    else:
        data["organizer"] = None

    # Ensure other tuple fields are tuples, not lists.
    for field in ("categories", "valarms"):
        if isinstance(data.get(field), list):
            data[field] = tuple(data[field])
    return Event(**{k: v for k, v in data.items() if k in Event.__dataclass_fields__})
