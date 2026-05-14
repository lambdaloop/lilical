# lilical — Architecture

Companion to [PLAN.md](PLAN.md). Describes module boundaries, the
`Backend` protocol, the async/threading model, error handling, and the
conventions that keep the layers honest.

---

## 1. Layers

```
┌───────────────────────────────────────────────────────────────────┐
│  ui/        Pure presentation. Knows about Qt. Knows the          │
│             EventStore + SyncEngine as interfaces, not classes.   │
├───────────────────────────────────────────────────────────────────┤
│  sync/      Orchestration. One asyncio.Task per account.          │
│  recurrence/                                                       │
│  storage/   Persistence + secrets. No Qt imports.                 │
├───────────────────────────────────────────────────────────────────┤
│  backends/  Network adapters. No DB, no Qt. Take `Event` in,      │
│             return `Event` + `SyncCursor` out.                    │
├───────────────────────────────────────────────────────────────────┤
│  models/    Plain data: dataclasses, SQLAlchemy declarative.      │
│             Imported by everyone. Imports nothing from us.        │
└───────────────────────────────────────────────────────────────────┘
```

### Dependency rule

Imports point **upward only**. A `backends/` module may not import from
`sync/`, `storage/`, or `ui/`. `storage/` may not import from `ui/`. Any
violation is a code smell; a `pyright` per-module-import allowlist
enforces it.

### Why three layers, not two

The temptation is to let the `SyncEngine` live in `backends/`. We resist
because the *engine* owns retry/backoff/conflict policy and the
*backends* own protocol knowledge. Keeping them separate means we can
unit-test the engine with a fake `Backend`, and unit-test a `Backend`
without spinning up the engine.

---

## 2. The `Backend` protocol

```python
class SyncCursor(Protocol):
    """Opaque per-backend resume token. Serializable to JSON."""
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> Self: ...


@dataclass(frozen=True, slots=True)
class EventChange:
    kind: Literal["upsert", "delete"]
    event: Event | None      # populated for upsert
    uid: str                 # always populated


class Backend(Protocol):
    account_id: str

    async def list_calendars(self) -> list[Calendar]: ...

    def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        """Async iterator over pages. Each yield is (page_changes,
        cursor_at_end_of_page); persist the cursor as you go so a
        partial initial sync can resume. The final yielded cursor is
        the resume point for `incremental_sync`."""

    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        """Since-cursor changes. Raises CursorExpired on 410/invalid."""

    async def create_event(self, calendar_id: str, event: Event) -> Event:
        """Returns the server-canonical event (with uid/etag filled)."""

    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        """if_match = previous etag for optimistic concurrency."""

    async def delete_event(
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None: ...
```

### Why a Protocol and not an ABC

We want backends to be ducktyped from test fakes without forcing
inheritance. Protocols are checked structurally by `pyright`.

### Exceptions the engine handles

| Exception              | Meaning                                | Engine action                  |
| ---------------------- | -------------------------------------- | ------------------------------ |
| `CursorExpired`        | 410 GONE, `deltaLink` expired, etc.    | Full re-sync of that calendar  |
| `AuthExpired`          | Refresh token revoked / 401            | Surface "reconnect" UI         |
| `ConflictError`        | Server rejected `If-Match`             | Queue 3-way merge              |
| `TransientError`       | 5xx, network, rate-limit               | Exponential backoff + retry    |
| `PermanentError`       | 4xx that isn't auth/conflict           | Log + skip event, surface pill |

---

## 3. Async + threading model

### One process, one event loop

We use **qasync** to run a single `asyncio` loop that drives both Qt
events and our network I/O. There is no `QThread`, no `ThreadPoolExecutor`,
no manual thread management *for code we write*.

```
                ┌───────────────────────┐
                │   QApplication exec   │
                │   (qasync.run loop)   │
                └──────────┬────────────┘
                           │
       ┌───────────────────┼───────────────────┐
       │                   │                   │
  Qt events          asyncio.Tasks       to_thread (rare)
  (clicks, paint)    one per account     RRULE expand,
                     sync poller         huge ICS import
```

### CPU-bound exceptions

A handful of operations can stall the loop:

1. **RRULE expansion** of a long-running series across a wide range.
2. **ICS file import** of a large `.ics`.
3. **SQLite query** of a wide date range with many materialized instances.

These run via `await asyncio.to_thread(...)`. We do *not* roll our own
thread pool.

### Cancellation discipline

Every long-running coroutine is cancellable. `SyncEngine.stop_account()`
calls `task.cancel()` and awaits the task; the coroutine must propagate
`CancelledError` (no bare `except Exception`).

### Shutdown

`QApplication.aboutToQuit` → engine stops all tasks → DB flushes →
`loop.close()`. Maestral's shutdown logic is the reference.

---

## 4. The `SyncEngine`

State machine per (account, calendar):

```
                     ┌────────────┐
                     │   IDLE     │◄────────────────┐
                     └──────┬─────┘                 │
                  schedule_tick │                   │ tick complete
                                ▼                   │
                      ┌─────────────────┐           │
                      │  SYNCING        │───────────┘
                      └─────┬───────┬───┘
            CursorExpired   │       │  AuthExpired
                            ▼       ▼
                ┌──────────────┐  ┌──────────────────┐
                │ FULL_RESYNC  │  │ NEEDS_RECONNECT  │
                └──────┬───────┘  └────────┬─────────┘
                       │                   │ user reconnects
                       └─────────┬─────────┘
                                 ▼
                            ┌────────┐
                            │  IDLE  │
                            └────────┘
```

Tick order in `SYNCING`:

1. **Drain pending writes** (table `pending_ops`, FIFO).
2. **Pull incremental changes** (`incremental_sync`).
3. **Apply changes** to `EventStore`, computing affected `event_instances`.
4. **Emit signal** `sync_finished(account_id, change_count)` for the UI.

### Backoff

Transient errors trigger exponential backoff: 5 s → 10 s → 20 s → 40 s →
80 s → 160 s, capped at 5 min. Each delay is multiplied by `random.uniform(0.5, 1.5)`
to jitter and avoid lockstep retry when several accounts fail together
(e.g. network outage). Reset on success.

### Scheduling

Default poll interval **5 minutes** per account. User can force a sync
via toolbar button (immediate tick).

### No global lock

Per-account isolation: a Google account hanging on a 30 s request does
not stall Outlook sync. Each account is an independent `asyncio.Task`.

---

## 5. UI ↔ Core wiring

### Signals out of Core

`EventStore` and `SyncEngine` are `QObject` subclasses (they live in
the non-UI layers — `storage/` and `sync/` respectively — but use Qt
signals to talk to the UI):

```python
class EventStore(QObject):
    events_changed = Signal(str, set)   # (calendar_id, set[uid])
    instances_changed = Signal(str, datetime, datetime)


class SyncEngine(QObject):
    sync_started = Signal(str)          # account_id
    sync_finished = Signal(str, int)    # account_id, n_changes
    sync_failed = Signal(str, str)      # account_id, message
    auth_expired = Signal(str)
    conflict_detected = Signal(str)     # uid
```

### Why Qt signals (vs callbacks vs an event bus)

- They cross the async/Qt boundary cleanly (qasync handles it).
- They're tooled (pyright understands `Signal`, Qt Designer too).
- They're the idiom every Qt developer reading our code expects.

### One-way data flow

The UI **never mutates `EventStore` directly**. To create an event:

```
User clicks New ──► UI calls EventStore.queue_create(event)
                    EventStore writes to pending_ops
                    SyncEngine notices, calls backend.create_event
                    Backend returns canonical Event
                    EventStore upserts, emits events_changed
                    UI rerenders the affected range
```

This is what makes offline-first work: every write goes through
`pending_ops` first, regardless of network state.

---

## 6. Error handling philosophy

### Catch where you can act

Backends raise typed exceptions (§2). The engine catches and acts. The
UI catches `ConflictError` from `EventStore.queue_update` only when it
needs to show a conflict dialog directly (which it usually doesn't —
conflicts surface via the `conflict_detected` signal).

### No bare `except Exception`

Every `except` either names the exception type or names `BaseException`
explicitly (only allowed in the top-level crash handler).

### One crash handler

`app.py` installs:

```python
sys.excepthook = _crash_to_journal
asyncio.get_event_loop().set_exception_handler(_crash_handler)
```

Both write to journald with a structured `lilical-crash` MESSAGE_ID so
users can pull crash details via `journalctl IDENTIFIER=lilical`.

### User-visible errors are pills

The bottom status bar has a slot for the latest error pill ("Couldn't
reach Google — retrying in 80 s"). Clicking it opens a details dialog
with the underlying exception text. We do not throw modal error dialogs
at users for transient errors.

---

## 7. Dependency injection

We avoid a DI framework. Instead, `app.py` is a manual composition root:

```python
def build_application() -> QApplication:
    app = QApplication(sys.argv)

    config = Config.load()
    secrets = SecretsStore.open(config)
    db_engine = create_engine(config.db_url, echo=False)
    Base.metadata.create_all(db_engine)  # alembic in prod
    event_store = EventStore(db_engine)
    recurrence = RecurrenceExpander(event_store)
    sync_engine = SyncEngine(
        event_store, secrets, backends_factory(secrets)
    )
    notifier = NotificationScheduler(event_store, recurrence)

    window = MainWindow(event_store, sync_engine, recurrence, config)
    window.show()
    return app
```

Each class declares its dependencies as constructor arguments. Tests
pass fakes. No global registry.

---

## 8. Configuration

Two layers:

1. **`config.toml`** in `$XDG_CONFIG_HOME/lilical/` — user-editable,
   captured by `Config` dataclass. Examples: default view, week starts on,
   poll interval, theme.
2. **`QSettings`** for UI state — window geometry, last open view,
   sidebar widths. Not user-editable.

We do not auto-write `config.toml`. The user edits it or uses the
Preferences dialog (which writes it atomically via tempfile + rename).

---

## 9. Logging

```
                ┌──────────────────────┐
                │  logging (Python)    │
                └──────────┬───────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   journald          stderr (dev)        crash file
   (production)      under pixi          (opt-in)
```

- Default level **INFO**, override via `$LILICAL_LOG_LEVEL`.
- Module loggers: `lilical.sync.engine`, `lilical.backends.google`, etc.
- Sensitive values (tokens, passwords) are **never** logged. The
  `SecretsStore` wraps tokens in a `Redacted[str]` newtype whose `__repr__`
  is `"***"`.

---

## 10. Conventions

- **Type hints everywhere.** `basedpyright` strict mode.
- **No `Any`**, except at the JSON boundary (use `cast()` immediately).
- **`dataclass(frozen=True, slots=True)` for value objects.**
- **Module-level immutable constants** are `UPPER_SNAKE`.
- **No mutable default arguments.**
- **Async naming**: every coroutine is `async def`; we do not return
  raw coroutines from sync functions.
- **Qt class naming**: PascalCase Qt widget subclasses live in `ui/`,
  never in `storage/` / `sync/` / `backends/` / `recurrence/` (the
  only exceptions are the `QObject` signal carriers in §5, which use
  Qt only for the signal mechanism — not widgets).
- **Imports**: standard lib → third-party → first-party, separated by
  blank lines, sorted by ruff.

---

## 11. Module-level invariants we check in CI

| Invariant                                           | Tool                          |
| --------------------------------------------------- | ----------------------------- |
| Layer dependency rule                               | `ruff` `flake8-tidy-imports`  |
| No raw `print()` outside `__main__`                 | `ruff`                        |
| No `time.sleep` in `async def`                      | `ruff` `async` rules          |
| No `subprocess` calls                               | `ruff` `flake8-bandit`        |
| All public functions/classes typed                  | `basedpyright` strict         |
| No floating `asyncio.create_task` (must be tracked) | custom ruff plugin or review  |

---

## 12. Extension points

Anticipated v0.2+ extensions and where they plug in:

| Feature                  | Where it goes                                       |
| ------------------------ | --------------------------------------------------- |
| Free-busy lookup         | New `Backend.free_busy()` method                    |
| Tasks                    | New `TaskStore` parallel to `EventStore`; backends gain `list_tasks` etc. |
| RSVP / invitations       | `Event.attendees` already in model; UI dialog only  |
| Search                   | SQLite FTS5 virtual table over `summary`, `description`, `location` |
| Shared calendars         | `Calendar.shared_with`, `Calendar.access_role` columns + UI |
| KDE/GNOME shell widget   | Separate process talking to the existing DB         |
| Mobile (Kirigami)        | Sibling app reusing `models/`, `storage/`, `backends/` |
