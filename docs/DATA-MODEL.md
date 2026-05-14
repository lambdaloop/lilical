# lilical — Data Model

Companion to [PLAN.md](PLAN.md) and [ARCHITECTURE.md](ARCHITECTURE.md).
Describes the SQLite schema, the JSCalendar-shaped in-memory model, the
materialized recurrence view, indexes, and migration strategy.

---

## 1. Storage location

```
$XDG_DATA_HOME/lilical/
├── lilical.db                # SQLite (WAL) — main store
├── lilical.db-wal            # write-ahead log
├── lilical.db-shm            # shared memory
├── credentials.enc           # fallback if no keyring
└── attachments/              # v0.2; not used in v0.1
```

`PRAGMA journal_mode=WAL` enabled at connection setup.
`PRAGMA foreign_keys=ON`. `PRAGMA synchronous=NORMAL` (safe with WAL).
`PRAGMA busy_timeout=5000`.

---

## 2. ER overview

```
┌──────────────┐ 1   * ┌──────────────┐ 1   * ┌──────────────┐
│   accounts   │───────│  calendars   │───────│    events    │
└──────────────┘       └──────────────┘       └──────┬───────┘
                                                     │ 1
                                                     │
                                                     │ *
                                              ┌──────▼───────────┐
                                              │ event_instances  │  (materialized)
                                              └──────────────────┘

┌──────────────┐
│ pending_ops  │  (write queue, references accounts.id + events.uid)
└──────────────┘

┌──────────────┐
│   settings   │  (singleton key/value)
└──────────────┘
```

---

## 3. Tables

### 3.1 `accounts`

| Column            | Type     | Constraints                              |
| ----------------- | -------- | ---------------------------------------- |
| `id`              | TEXT PK  | uuid4                                    |
| `kind`            | TEXT     | `'google' \| 'graph' \| 'caldav'`        |
| `display_name`    | TEXT     | NOT NULL                                 |
| `identity`        | TEXT     | email (Google/Graph) or username (CalDAV) |
| `server_url`      | TEXT     | NULL for Google/Graph, base URL for CalDAV |
| `secret_ref`      | TEXT     | opaque keyring slot id                   |
| `created_at`      | TEXT     | ISO-8601 UTC                             |
| `enabled`         | INTEGER  | 0/1; soft-disable without deletion       |

### 3.2 `calendars`

| Column            | Type     | Constraints                              |
| ----------------- | -------- | ---------------------------------------- |
| `id`              | TEXT PK  | uuid4 (local; not the provider id)       |
| `account_id`      | TEXT FK  | → accounts.id, ON DELETE CASCADE         |
| `provider_id`     | TEXT     | the calendarId / collection path / etc. |
| `display_name`    | TEXT     |                                          |
| `color`           | TEXT     | hex `#RRGGBB`                            |
| `is_primary`      | INTEGER  | 0/1                                      |
| `is_visible`      | INTEGER  | 0/1; user toggle in sidebar              |
| `is_favorite`     | INTEGER  | 0/1; Business-Calendar-style favorite bar |
| `access_role`     | TEXT     | `'owner' \| 'writer' \| 'reader'`        |
| `sync_cursor`     | TEXT     | JSON-serialized SyncCursor               |
| `last_synced_at`  | TEXT     | ISO-8601 UTC                             |

Unique: `(account_id, provider_id)`.

### 3.3 `events`

| Column            | Type     | Notes                                                  |
| ----------------- | -------- | ------------------------------------------------------ |
| `uid`             | TEXT PK  | RFC 5545 UID (globally unique; stable across moves)    |
| `calendar_id`     | TEXT FK  | → calendars.id, ON DELETE CASCADE                      |
| `provider_event_id` | TEXT   | provider-specific id (Google id, Graph id, CalDAV href) |
| `dtstart`         | TEXT     | ISO-8601 with tz suffix (`2026-05-13T14:00:00+02:00`)  |
| `dtend`           | TEXT     | same                                                   |
| `tz`              | TEXT     | IANA zone (`Europe/Berlin`); kept separately for RRULE |
| `all_day`         | INTEGER  | 0/1                                                    |
| `summary`         | TEXT     |                                                        |
| `description`     | TEXT     |                                                        |
| `location`        | TEXT     |                                                        |
| `url`             | TEXT     | e.g. video-call link                                   |
| `rrule`           | TEXT     | raw `RRULE:` string                                    |
| `recurrence_id`   | TEXT     | for instance overrides; ISO-8601 datetime              |
| `exdates`         | TEXT     | JSON list of ISO-8601                                  |
| `rdates`          | TEXT     | JSON list of ISO-8601                                  |
| `attendees`       | TEXT     | JSON: `[{email, name, role, partstat, is_organizer}]`  |
| `categories`      | TEXT     | JSON list of strings                                   |
| `color`           | TEXT     | hex; falls back to calendar color if NULL              |
| `status`          | TEXT     | `'CONFIRMED' \| 'TENTATIVE' \| 'CANCELLED'`            |
| `transparency`    | TEXT     | `'OPAQUE' \| 'TRANSPARENT'`; used for free-busy        |
| `valarms`         | TEXT     | JSON list of `{trigger_offset_minutes, action}`        |
| `etag`            | TEXT     | server etag (or sequence-derived for Google)           |
| `sequence`        | INTEGER  | iCal SEQUENCE                                          |
| `last_modified`   | TEXT     | server's LAST-MODIFIED, ISO-8601                       |
| `local_dirty`     | INTEGER  | 0/1                                                    |
| `deleted_locally` | INTEGER  | 0/1; tombstone awaiting confirmation                   |
| `conflict_state`  | TEXT     | `NULL \| 'NEEDS_USER' \| 'RESOLVING'`                  |
| `local_modified_at` | TEXT   | ISO-8601 UTC; updated on local edit                    |
| `inserted_at`     | TEXT     | ISO-8601 UTC                                           |

UID is global (RFC 5545), but a single PK on `uid` doesn't work for us:

1. **Same UID across accounts.** When the same event is invited to
   multiple Google + Outlook calendars, both copies legitimately
   carry the same UID. We need separate rows.
2. **RECURRENCE-ID overrides.** A single edited occurrence of a
   recurring series is stored as its own event row that shares
   `uid` *and* `calendar_id` with its master series, distinguished
   only by `recurrence_id`.

Composite PK: `PRIMARY KEY (uid, calendar_id, recurrence_id)`, with
`recurrence_id = ''` (empty string, not NULL — SQLite treats NULL as
distinct per row, so we'd lose uniqueness) for the master / non-
recurring row. The application layer translates between NULL (in the
in-memory `Event`) and `''` (on disk).

We expose `uid` as the public handle. Foreign keys from
`event_instances`, `pending_ops` etc. carry the full
`(uid, calendar_id, recurrence_id)` triple.

Additional uniqueness: `UNIQUE (calendar_id, provider_event_id)`,
because the remote-id lookup path (when applying changes from a sync
response) must be deterministic.

### 3.4 `event_instances` (materialized recurrence view)

For fast range queries, we expand every event's recurrence into concrete
occurrences within a bounded window (±2 years from today). Rebuilt
incrementally on event changes; rebuilt fully on app startup if the
window has shifted.

| Column            | Type     | Notes                                              |
| ----------------- | -------- | -------------------------------------------------- |
| `id`              | INTEGER PK AUTOINCREMENT |                              |
| `uid`             | TEXT     | FK → events.uid (composite via calendar_id)        |
| `calendar_id`     | TEXT     | FK → calendars.id                                  |
| `dtstart_utc`     | INTEGER  | Unix epoch seconds; range queries use this         |
| `dtend_utc`       | INTEGER  | Unix epoch seconds                                 |
| `dtstart_local`   | TEXT     | ISO-8601 with tz (for display)                     |
| `dtend_local`     | TEXT     | ISO-8601 with tz                                   |
| `all_day`         | INTEGER  | 0/1                                                |
| `is_override`     | INTEGER  | 0/1; this instance has a RECURRENCE-ID override    |

Why epoch ints for the range columns: SQLite range queries on TEXT
ISO-8601 work but integer comparisons are faster and the BETWEEN
predicate compiles to a direct index seek.

### 3.5 `pending_ops`

The write queue. SyncEngine drains FIFO.

| Column            | Type     | Notes                                              |
| ----------------- | -------- | -------------------------------------------------- |
| `id`              | INTEGER PK AUTOINCREMENT |                              |
| `account_id`      | TEXT FK  |                                                    |
| `calendar_id`     | TEXT FK  |                                                    |
| `uid`             | TEXT     |                                                    |
| `op`              | TEXT     | `'create' \| 'update' \| 'delete'`                 |
| `payload`         | TEXT     | JSON snapshot of the Event at queue-time           |
| `if_match`        | TEXT     | etag we believed was current                       |
| `attempts`        | INTEGER  | retry counter                                      |
| `last_attempt_at` | TEXT     | for backoff                                        |
| `last_error`      | TEXT     | for the error pill                                 |
| `created_at`      | TEXT     |                                                    |

### 3.6 `settings`

Singleton key/value for app-state we don't want in `config.toml`:

| Column            | Type     |
| ----------------- | -------- |
| `key`             | TEXT PK  |
| `value`           | TEXT     |

E.g. `instances_window_start`, `instances_window_end`, `schema_version`.

---

## 4. Indexes

```sql
CREATE INDEX idx_events_calendar       ON events(calendar_id);
CREATE INDEX idx_events_dirty          ON events(local_dirty) WHERE local_dirty=1;
CREATE INDEX idx_events_deleted        ON events(deleted_locally) WHERE deleted_locally=1;
CREATE INDEX idx_events_conflict       ON events(conflict_state) WHERE conflict_state IS NOT NULL;

CREATE INDEX idx_instances_range       ON event_instances(dtstart_utc, dtend_utc);
CREATE INDEX idx_instances_calendar    ON event_instances(calendar_id, dtstart_utc);
CREATE INDEX idx_instances_uid         ON event_instances(uid, calendar_id);

CREATE INDEX idx_pending_account       ON pending_ops(account_id, created_at);

CREATE UNIQUE INDEX uq_calendars_provider
    ON calendars(account_id, provider_id);

CREATE UNIQUE INDEX uq_events_provider
    ON events(calendar_id, provider_event_id);
```

Partial indexes on `local_dirty=1`, `deleted_locally=1`, and
`conflict_state IS NOT NULL` keep the indexes tiny (we expect <100 dirty
rows at any time even on huge calendars).

---

## 5. The in-memory `Event`

```python
@dataclass(frozen=True, slots=True)
class Event:
    uid: str
    calendar_id: str
    provider_event_id: str | None
    dtstart: datetime           # tz-aware
    dtend: datetime             # tz-aware
    tz: str                     # IANA name
    all_day: bool
    summary: str
    description: str
    location: str
    url: str | None
    rrule: str | None
    recurrence_id: datetime | None
    exdates: tuple[datetime, ...]
    rdates: tuple[datetime, ...]
    attendees: tuple[Attendee, ...]
    categories: tuple[str, ...]
    color: str | None
    status: EventStatus
    transparency: Transparency
    valarms: tuple[VAlarm, ...]
    etag: str | None
    sequence: int
    last_modified: datetime | None
    local_dirty: bool = False
    deleted_locally: bool = False
    conflict_state: ConflictState | None = None
```

`Event` is **immutable**. Edits produce a new `Event` via `dataclasses.replace`.
This sidesteps a whole category of "someone mutated the object that the
UI was still rendering" bugs.

### JSCalendar mapping

Where field names diverge, here's the mapping:

| JSCalendar           | lilical `Event`           |
| -------------------- | ------------------------- |
| `@type`              | implicit (always Event)   |
| `uid`                | `uid`                     |
| `title`              | `summary`                 |
| `description`        | `description`             |
| `start`              | `dtstart`                 |
| `duration`           | derived from `dtstart`/`dtend` |
| `timeZone`           | `tz`                      |
| `showWithoutTime`    | `all_day`                 |
| `recurrenceRules`    | `rrule` (single rule v0.1) |
| `recurrenceOverrides`| handled via separate event rows with `recurrence_id` set |
| `excludedRecurrenceRules` | n/a v0.1             |
| `participants`       | `attendees`               |
| `priority`           | n/a v0.1                  |
| `freeBusyStatus`     | `transparency`            |
| `status`             | `status`                  |
| `alerts`             | `valarms`                 |

We are **not** strictly JSCalendar-on-the-wire — we store iCalendar-y
text fields (`rrule` as the raw RRULE string) because that's what
backends speak. The JSCalendar shape is just our naming guide.

---

## 6. Provider id ↔ UID

| Backend     | UID source                                                  |
| ----------- | ----------------------------------------------------------- |
| Google      | `event["iCalUID"]` (Google preserves these)                 |
| Graph       | `event["iCalUId"]`                                          |
| CalDAV      | parsed from VEVENT `UID:` property                          |

`provider_event_id` is what we need to PATCH/DELETE:

| Backend     | provider_event_id                                           |
| ----------- | ----------------------------------------------------------- |
| Google      | `event["id"]`                                               |
| Graph       | `event["id"]`                                               |
| CalDAV      | href of the iCalendar resource (path under collection)      |

When a backend gives us an event with a brand-new UID, we INSERT. When
we already have a row with that `(uid, calendar_id)`, we UPDATE in place
and preserve our local PK semantics.

---

## 7. Migrations — Alembic

Alembic is the standard for SQLAlchemy schema migrations. We use it
from day one even with one revision in `migrations/versions/`, because
adding it after the fact is painful.

Conventions:

- **One revision per pull request** that changes the schema.
- Filenames `2026_05_13_a1b2_add_color_column.py` (date + short hash + slug).
- `downgrade()` is implemented; we don't ship one-way migrations.
- `pixi run migrate` = `alembic upgrade head` at boot if needed.
- The app refuses to start if `head` doesn't match the DB; suggests
  `pixi run migrate` in the error.

### Tricky migrations

For columns that need backfill (e.g. computing `dtstart_utc` for existing
rows), the migration ships a one-shot script that uses SQLAlchemy
Core (not the ORM models, which may have moved on).

---

## 8. The `event_instances` rebuild policy

The materialized view is bounded to `[today - 1y, today + 1y]` by default
(configurable). Rebuild triggers:

| Trigger                                       | Rebuild scope                    |
| --------------------------------------------- | -------------------------------- |
| App startup, window has shifted by >7 days    | Drop+rebuild all                 |
| Single event INSERT/UPDATE/DELETE             | Re-expand that uid only          |
| RRULE/EXDATE/RDATE of an event changed        | Re-expand that uid only          |
| Calendar visibility toggled                   | No rebuild (UI filter, not DB)   |
| Settings change `instances_window_start/end`  | Drop+rebuild all                 |

Re-expand-one-event runs in `asyncio.to_thread()` to keep the loop hot;
a "rebuild progress" pill appears in the status bar for full rebuilds.

---

## 9. Secrets — `credentials.enc` format

When the OS keyring is unavailable, we fall back to a single encrypted
blob. Format (versioned):

```
LILICAL_CRED_V1\n
<base64 nonce, 12 bytes>\n
<base64 salt, 16 bytes>\n
<base64 ciphertext + tag (AES-256-GCM)>
```

- KDF: Argon2id, `t=3, m=64MiB, p=1`. (`argon2-cffi` from conda-forge.)
- Cipher: AES-256-GCM (`cryptography` from conda-forge).
- Cleartext is JSON `{account_id: {refresh_token: …, …}, …}`.
- File mode `0600`. We `fchmod` after `open` to avoid race window.
- Passphrase prompted via a Qt dialog at startup if file exists; held in
  memory only.

---

## 10. Schema versioning at the app layer

`settings.schema_version` is bumped by each migration. The app's
`__main__` checks:

```python
if settings["schema_version"] != EXPECTED_SCHEMA_VERSION:
    show_error_and_exit(
        "Database schema is out of date. Run `pixi run migrate`."
    )
```

This prevents a user with an old binary opening a newer DB and getting
weird errors. Production Flatpak users won't hit this because the
Flatpak ships `alembic upgrade head` in its launcher script.

---

## 11. Capacity & query expectations

Expected scale of a power-user calendar:

| Quantity                    | Order of magnitude    |
| --------------------------- | --------------------- |
| Accounts                    | 1–5                   |
| Calendars total             | 5–30                  |
| Events stored               | 5 000 – 50 000        |
| Recurring event series      | 100 – 2 000           |
| Expanded instances (±1 yr)  | 10 000 – 200 000      |
| Pending ops at any moment   | < 100                 |

Performance budgets for v0.1:

| Operation                                        | Budget   |
| ------------------------------------------------ | -------- |
| Query Month view (35 days × all calendars)       | < 50 ms  |
| Query Week view                                  | < 30 ms  |
| Query Day view                                   | < 15 ms  |
| Insert one event + queue pending_op              | < 20 ms  |
| Re-expand a yearly RRULE                         | < 100 ms |
| Initial sync of a 5 000-event Google calendar    | < 60 s   |
| Incremental sync (no changes)                    | < 1 s    |
| Cold start to first frame                        | < 1.5 s  |

These are not stretch goals; they fall out of correct indexing and
WAL mode on modern hardware. The 60 s initial-sync budget assumes
sequential pagination at 250 events/page; for slow networks the
backend may issue up to 4 concurrent page fetches (capped by the
provider's rate limit). Tighter parallelism is a v0.2 question
once we have real telemetry.

---

## 12. What we don't store

We deliberately do not store:

- **Attachments.** v0.2; will live in `attachments/` outside the DB.
- **Free-busy data for other users.** Computed at query time.
- **Search index for v0.1.** FTS5 added when v0.2 search lands.
- **Network response cache.** Sync tokens are the cache.
- **Analytics, usage telemetry, error reports.** None. Ever.
