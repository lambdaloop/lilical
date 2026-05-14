# lilical — Implementation Guide

Companion to [PLAN.md](PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[DATA-MODEL.md](DATA-MODEL.md), [UI-SPEC.md](UI-SPEC.md).

This doc is the "how" — concrete code patterns, algorithms, and test
fixtures for each subsystem. Written so a small implementer (junior
dev or LLM agent) can implement v0.1 by reading top-to-bottom. Sections
are ordered by implementation dependency: §1 first, §18 last.

If something is in PLAN/ARCHITECTURE/DATA-MODEL/UI-SPEC, this doc
does not repeat it — only the *implementation* of it. Cross-reference
liberally.

---

## 0. Universal conventions

### 0.1 Datetimes

**Every `datetime` in lilical is timezone-aware.** Naive datetimes are
a bug; `pyright` + a `_check_aware` helper at the boundaries enforces
it.

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def _check_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    return dt

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def to_utc(dt: datetime) -> datetime:
    return _check_aware(dt).astimezone(timezone.utc)

def in_zone(dt: datetime, zone: str) -> datetime:
    return _check_aware(dt).astimezone(ZoneInfo(zone))
```

### 0.2 All-day events

All-day events are stored as **midnight-to-midnight in the event's
local timezone**, with `all_day=True` and a separate code path for
rendering. The iCalendar wire format is `DTSTART;VALUE=DATE:YYYYMMDD`
(no time, no zone). We convert at the backend boundary:

```python
def parse_dtstart(value, params) -> tuple[datetime, str, bool]:
    """Returns (dtstart, tz, all_day)."""
    if params.get("VALUE") == "DATE":
        d = date.fromisoformat(value)
        return (
            datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("UTC")),
            "UTC",
            True,
        )
    # …time-of-day cases
```

### 0.3 Storage: ISO-8601 with offset

On disk (`events.dtstart`), store the ISO-8601 string *with* the
offset suffix: `2026-05-13T14:00:00+02:00`. The IANA name lives in
the parallel `tz` column for RRULE expansion (offsets aren't enough
because DST transitions depend on the named zone).

For range queries we use the `event_instances` table with **integer
epoch seconds** (`dtstart_utc`, `dtend_utc`), see DATA-MODEL §3.4.

### 0.4 Logging

```python
import logging

log = logging.getLogger(__name__)   # at module top in every file

# in app.py:
def setup_logging() -> None:
    level = os.environ.get("LILICAL_LOG_LEVEL", "INFO")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        from systemd.journal import JournalHandler
        handlers.append(JournalHandler(SYSLOG_IDENTIFIER="lilical"))
    except ImportError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
```

### 0.5 Never log secrets

```python
class Redacted:
    __slots__ = ("_value",)
    def __init__(self, value: str) -> None: self._value = value
    def reveal(self) -> str: return self._value
    def __repr__(self) -> str: return "***"
    __str__ = __repr__
```

All token / password fields carry `Redacted[str]` in dataclasses.

---

## 1. Bootstrapping (`app.py`)

The composition root and the qasync boot sequence. This is delicate —
get it wrong and you get either a frozen UI or a leaked event loop on
shutdown.

```python
# src/lilical/__main__.py
from lilical.app import main
raise SystemExit(main())
```

```python
# src/lilical/app.py
import asyncio, signal, sys
import qasync
from PySide6.QtWidgets import QApplication

from lilical.config import Config
from lilical.logging_setup import setup_logging
from lilical.storage.db import open_engine, ensure_schema
from lilical.storage.event_store import EventStore
from lilical.storage.secrets import SecretsStore
from lilical.sync.engine import SyncEngine
from lilical.backends.factory import build_backend_factory
from lilical.recurrence.expander import RecurrenceExpander
from lilical.ui.main_window import MainWindow
from lilical.ui.notifications import NotificationScheduler


def main() -> int:
    setup_logging()

    config = Config.load()
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("lilical")
    qt_app.setOrganizationName("lilical")
    qt_app.setDesktopFileName("io.github.lilical.Lilical")

    loop = qasync.QEventLoop(qt_app)
    asyncio.set_event_loop(loop)

    db_engine = open_engine(config.db_path)
    ensure_schema(db_engine)   # raises if alembic head != db

    secrets = SecretsStore.open(config)
    event_store = EventStore(db_engine)
    recurrence = RecurrenceExpander(event_store)
    backend_factory = build_backend_factory(secrets)
    sync_engine = SyncEngine(event_store, secrets, backend_factory)
    notifier = NotificationScheduler(event_store, recurrence)

    window = MainWindow(
        config=config,
        event_store=event_store,
        sync_engine=sync_engine,
        recurrence=recurrence,
        secrets=secrets,
    )
    window.show()

    # Wire up shutdown
    async def _shutdown() -> None:
        await sync_engine.stop_all()
        await notifier.stop()
        loop.stop()

    qt_app.aboutToQuit.connect(lambda: asyncio.ensure_future(_shutdown()))

    # Start background work
    asyncio.ensure_future(sync_engine.start_all())
    asyncio.ensure_future(notifier.start())

    with loop:
        return loop.run_forever() or 0
```

### Key invariants

1. **Create the `QApplication` before `QEventLoop`.** qasync needs it.
2. **`asyncio.set_event_loop(loop)`** must happen before any
   `asyncio.ensure_future` call.
3. **Never call `asyncio.run`.** It would create a second loop.
4. **`aboutToQuit` is the shutdown hook**, not `closeEvent` (which
   fires per window).

---

## 2. Models (SQLAlchemy 2.x declarative)

```python
# src/lilical/models/db.py
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

```python
# src/lilical/models/account.py
from sqlalchemy import String, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

class Account(Base):
    __tablename__ = "accounts"
    id:           Mapped[str] = mapped_column(String, primary_key=True)
    kind:         Mapped[str] = mapped_column(String, nullable=False)  # google|graph|caldav
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    identity:     Mapped[str] = mapped_column(Text, nullable=False)
    server_url:   Mapped[str | None] = mapped_column(Text)
    secret_ref:   Mapped[str] = mapped_column(Text, nullable=False)
    created_at:   Mapped[str] = mapped_column(Text, nullable=False)
    enabled:      Mapped[int] = mapped_column(Integer, default=1)
```

```python
# src/lilical/models/event.py
from sqlalchemy import String, Integer, Text, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "provider_event_id",
                         name="uq_events_provider"),
        Index("idx_events_calendar", "calendar_id"),
        Index("idx_events_dirty", "local_dirty",
              sqlite_where="local_dirty=1"),
        Index("idx_events_deleted", "deleted_locally",
              sqlite_where="deleted_locally=1"),
        Index("idx_events_conflict", "conflict_state",
              sqlite_where="conflict_state IS NOT NULL"),
    )
    uid:             Mapped[str] = mapped_column(String, primary_key=True)
    calendar_id:     Mapped[str] = mapped_column(String,
                                                 ForeignKey("calendars.id",
                                                            ondelete="CASCADE"),
                                                 primary_key=True)
    recurrence_id:   Mapped[str] = mapped_column(String, primary_key=True,
                                                 default="")
    provider_event_id: Mapped[str | None] = mapped_column(Text)
    # … remaining columns per DATA-MODEL §3.3
```

(Repeat the pattern for `Calendar`, `EventInstance`, `PendingOp`,
`Setting`.)

### SQLite pragmas at connection

```python
# src/lilical/storage/db.py
from sqlalchemy import event, create_engine
from sqlalchemy.engine import Engine

def open_engine(db_path: str) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}",
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def ensure_schema(engine: Engine) -> None:
    """Validate alembic head == DB; raise SchemaOutOfDate otherwise."""
    from alembic.script import ScriptDirectory
    from alembic.config import Config as AlembicConfig
    cfg = AlembicConfig("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    expected = script.get_current_head()
    with engine.connect() as conn:
        actual = conn.exec_driver_sql(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).scalar()
    if actual != expected:
        raise SchemaOutOfDate(expected=expected, actual=actual)
```

### Initial Alembic migration sketch

```python
# migrations/versions/2026_05_13_0001_initial.py
"""initial schema"""
revision = "0001"
down_revision = None

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    op.create_table("accounts", …)
    op.create_table("calendars", …)
    op.create_table("events", …)
    op.create_table("event_instances", …)
    op.create_table("pending_ops", …)
    op.create_table("settings", …)
    op.execute("INSERT INTO settings(key,value) VALUES "
               "('schema_version','0001')")

def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("pending_ops")
    op.drop_table("event_instances")
    op.drop_table("events")
    op.drop_table("calendars")
    op.drop_table("accounts")
```

---

## 3. `EventStore`

```python
# src/lilical/storage/event_store.py
from datetime import datetime
from PySide6.QtCore import QObject, Signal

class EventStore(QObject):
    events_changed = Signal(str, set)    # calendar_id, set[uid]
    instances_changed = Signal(str, datetime, datetime)

    def __init__(self, engine) -> None:
        super().__init__()
        self._engine = engine

    # ---------------- range queries ----------------
    def list_instances(self,
                       start_utc: datetime,
                       end_utc: datetime,
                       calendar_ids: set[str] | None = None,
                       ) -> list[EventInstance]:
        """Hot path — must hit idx_instances_range."""
        with Session(self._engine) as s:
            q = s.query(EventInstanceRow).filter(
                EventInstanceRow.dtstart_utc < int(end_utc.timestamp()),
                EventInstanceRow.dtend_utc   > int(start_utc.timestamp()),
            )
            if calendar_ids is not None:
                q = q.filter(EventInstanceRow.calendar_id.in_(calendar_ids))
            return [_row_to_instance(r) for r in q.all()]

    # ---------------- mutations ----------------
    def queue_create(self, event: Event) -> None:
        """Write the event row dirty + a pending_op. Atomic."""
        with Session(self._engine) as s, s.begin():
            row = _event_to_row(event)
            row.local_dirty = True
            s.add(row)
            s.add(PendingOpRow(
                account_id=row.account_id,
                calendar_id=row.calendar_id,
                uid=row.uid,
                op="create",
                payload=event.to_json(),
                if_match=None,
                attempts=0,
                created_at=utc_now().isoformat(),
            ))
            self._rebuild_instances_for(s, event)
        self.events_changed.emit(event.calendar_id, {event.uid})

    def queue_update(self, event: Event, prev_etag: str | None) -> None: …
    def queue_delete(self, uid: str, calendar_id: str,
                     recurrence_id: str | None) -> None: …

    # ---------------- server-side apply ----------------
    def apply_remote_changes(
        self,
        calendar_id: str,
        changes: list[EventChange],
        new_cursor: SyncCursor,
    ) -> None: …
```

The `_rebuild_instances_for` is what makes range queries fast — see
§9 for the recurrence-expansion call.

---

## 4. `Backend` protocol — the concrete shape

The canonical signature is in [ARCHITECTURE §2](ARCHITECTURE.md#2-the-backend-protocol).
Here we add: when each method is called, what it must accept, and
what error contract it satisfies.

### Lifecycle

1. `account_setup_wizard` creates an `Account` row + writes secrets.
2. `SyncEngine.start_all()` → for each enabled account, instantiate
   the right backend class via the factory, then call
   `list_calendars` and persist any new ones.
3. Per-calendar: if `sync_cursor` IS NULL → `initial_sync`,
   else `incremental_sync`.

### Error contract

Every backend method either:
- returns normally, or
- raises one of `CursorExpired`, `AuthExpired`, `ConflictError`,
  `TransientError`, `PermanentError`.

**Anything else escaping a backend is a bug.** Add a wrapping decorator:

```python
def _classify_errors(f):
    @functools.wraps(f)
    async def wrapper(*a, **kw):
        try:
            return await f(*a, **kw)
        except (TimeoutError, aiohttp.ClientError) as e:
            raise TransientError(str(e)) from e
        except CursorExpired:
            raise
        except Exception as e:
            log.exception("unclassified backend error in %s", f.__name__)
            raise PermanentError(str(e)) from e
    return wrapper
```

---

## 5. CalDAV backend

### Discovery

```
1. User gives:  base_url (e.g. https://nextcloud.example/remote.php/dav)
                username, app_password
2. GET /.well-known/caldav → 301 to actual DAV root (often)
3. PROPFIND on DAV root for current-user-principal:
     <D:propfind><D:prop><D:current-user-principal/></D:prop></D:propfind>
4. PROPFIND on principal URL for calendar-home-set:
     <D:propfind><D:prop>
       <C:calendar-home-set xmlns:C="urn:ietf:params:xml:ns:caldav"/>
     </D:prop></D:propfind>
5. PROPFIND Depth: 1 on home-set for each calendar:
     resourcetype, displayname, calendar-color, supported-component-set,
     getctag, sync-token, current-user-privilege-set
6. Filter resourcetype to <C:calendar/>; persist.
```

The `caldav` library does most of this — `DAVClient(...).principal().calendars()` —
but you need to fall back to manual PROPFINDs for servers that don't
expose `.well-known/caldav` (older Radicale).

### Sync

`caldav.Calendar.objects_by_sync_token(sync_token, load_objects=True)`
implements RFC 6578. For servers that don't advertise sync-collection
support (e.g. some Apple-style), fall back to CTAG-based: store
`getctag`, compare on next poll, if changed do a full Depth:1 PROPFIND
diff against existing hrefs.

### VEVENT → Event

```python
import icalendar

def vevent_to_event(ve: icalendar.Event, *, calendar_id: str,
                    href: str, etag: str) -> Event:
    dt_start_prop = ve.get("DTSTART")
    dt_end_prop   = ve.get("DTEND") or ve.get("DURATION")
    all_day = dt_start_prop.params.get("VALUE") == "DATE"
    tz = (dt_start_prop.params.get("TZID")
          or _zone_from_value(dt_start_prop.dt)
          or "UTC")
    return Event(
        uid=str(ve["UID"]),
        calendar_id=calendar_id,
        provider_event_id=href,
        dtstart=_to_aware(dt_start_prop.dt, tz),
        dtend=_dtend(dt_start_prop, dt_end_prop, tz),
        tz=tz,
        all_day=all_day,
        summary=str(ve.get("SUMMARY", "")),
        description=str(ve.get("DESCRIPTION", "")),
        location=str(ve.get("LOCATION", "")),
        url=str(ve.get("URL", "")) or None,
        rrule=str(ve["RRULE"].to_ical().decode()) if "RRULE" in ve else None,
        recurrence_id=_to_aware(ve["RECURRENCE-ID"].dt, tz)
                      if "RECURRENCE-ID" in ve else None,
        exdates=_exdates(ve),
        rdates=_rdates(ve),
        attendees=_attendees(ve),
        categories=tuple(str(c) for c in ve.get("CATEGORIES", [])),
        color=str(ve.get("COLOR", "")) or None,
        status=_status(ve),
        transparency=_transp(ve),
        valarms=_valarms(ve),
        etag=etag,
        sequence=int(ve.get("SEQUENCE", 0)),
        last_modified=_to_aware(ve["LAST-MODIFIED"].dt, "UTC")
                      if "LAST-MODIFIED" in ve else None,
    )
```

`Event → VEVENT` is the inverse (build an `icalendar.Event`, set
properties, return the rendered iCalendar string for PUT).

### Per-server quirk list

| Server               | Quirk                                                   | Shim                                                          |
| -------------------- | ------------------------------------------------------- | ------------------------------------------------------------- |
| **Radicale**         | None significant — reference                            | n/a                                                           |
| **Nextcloud**        | Returns 207 with empty multistatus on empty calendars   | Treat as zero events, not an error                            |
| **iCloud**           | Requires app-specific password; principal URL is at `https://caldav.icloud.com/<id>/principal/` | Probe principal endpoint variants in discovery |
| **iCloud**           | Strips unknown X- properties                            | Don't round-trip custom X-props through iCloud                |
| **Fastmail**         | `.well-known/caldav` returns 401 before auth            | Send credentials on the discovery request                     |
| **Fastmail**         | UID-only PUT path is unstable                           | Always PUT to the href the server returned                    |
| **Google CalDAV**    | Limited (no shared cals, no notifications); CalDAV is a poor man's API | Prefer the native Google backend; only allow CalDAV if user insists |
| **Google CalDAV**    | etag comparison fails when scheduling-changed prop is appended | Re-fetch on If-Match mismatch                       |
| **Older Radicale (<3.x)** | No sync-collection                                 | Fall back to CTAG diff                                        |

Implement shims as a small registry:

```python
QUIRKS = {
    "nextcloud": NextcloudShim,
    "icloud":    iCloudShim,
    "fastmail":  FastmailShim,
    "google":    GoogleCalDAVShim,
    None:        DefaultShim,
}

def detect_server(base_url: str, response_headers: dict) -> str | None:
    """Sniff from base_url and Server header. Heuristics only."""
```

---

## 6. Google backend

### OAuth setup

- **Application type**: Desktop app.
- **Client ID**: shipped in `src/lilical/backends/_google_client.json`
  (no client secret needed for PKCE).
- **Scopes**:
  ```
  https://www.googleapis.com/auth/calendar
  https://www.googleapis.com/auth/calendar.events
  ```
- **Loopback redirect**: `http://127.0.0.1:<port>/`, where `<port>` is
  picked at flow start by binding to port 0.

### Loopback flow (using `google-auth-oauthlib`)

```python
from google_auth_oauthlib.flow import InstalledAppFlow

def google_oauth_flow() -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(
        _CLIENT_JSON_PATH,
        scopes=[
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
        ],
    )
    # Opens browser, blocks until loopback receives the redirect.
    # `port=0` asks the OS for a free port.
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="Authorize lilical in your browser.",
        success_message="Authorized! You can close this tab.",
    )
    return creds
```

**Run on the qasync loop without blocking** by wrapping in
`asyncio.to_thread()` — the `run_local_server` call is synchronous.

### Token persistence

```python
{
    "refresh_token": "...",
    "client_id":     "...",
    "token_uri":     "https://oauth2.googleapis.com/token",
    "scopes":        [...],
}
```

Stored under `accounts.secret_ref = "google:{account_id}"` in the
keyring. We do **not** persist the access token — refresh on every
session start.

### syncToken usage

```python
service = build("calendar", "v3", credentials=creds, cache_discovery=False)

# Initial sync — no syncToken; paginate fully
req = service.events().list(
    calendarId=cal_id,
    singleEvents=False,     # we want the master series, not expansions
    showDeleted=True,
    maxResults=250,
)
while req is not None:
    resp = req.execute()
    for ev_json in resp.get("items", []):
        yield event_change_from_google(ev_json)
    if "nextPageToken" in resp:
        req = service.events().list_next(req, resp)
    else:
        # last page has nextSyncToken
        save_cursor(resp["nextSyncToken"])
        req = None

# Incremental — pass syncToken
req = service.events().list(
    calendarId=cal_id,
    syncToken=stored_token,
    singleEvents=False,
    showDeleted=True,
)
try:
    resp = req.execute()
except HttpError as e:
    if e.resp.status == 410:
        raise CursorExpired() from e
    raise
```

### JSON → Event mapping

| Google field                  | `Event` attribute          |
| ----------------------------- | -------------------------- |
| `iCalUID`                     | `uid`                      |
| `id`                          | `provider_event_id`        |
| `summary`                     | `summary`                  |
| `description`                 | `description`              |
| `location`                    | `location`                 |
| `htmlLink`                    | `url`                      |
| `start.dateTime` / `start.date` + `start.timeZone` | `dtstart`, `tz`, `all_day` |
| `end.dateTime` / `end.date`   | `dtend`                    |
| `recurrence[0]` (the RRULE)   | `rrule`                    |
| `recurrence[1..]` (EXDATE, RDATE) | `exdates`, `rdates`    |
| `originalStartTime.dateTime`  | `recurrence_id`            |
| `attendees`                   | `attendees`                |
| `status`                      | `status` (`confirmed` → `CONFIRMED`) |
| `transparency`                | `transparency`             |
| `reminders.overrides`         | `valarms`                  |
| `etag`                        | `etag`                     |
| `sequence`                    | `sequence`                 |
| `updated`                     | `last_modified`            |
| `colorId`                     | `color` (resolve via `colors().get()`) |

**Deletion detection:** Google returns `status: "cancelled"` and
sometimes a minimal `{"id", "status", "iCalUID"}` body — yield an
`EventChange(kind="delete", uid=…)`.

### Write — create

```python
body = event_to_google(event)
resp = service.events().insert(
    calendarId=cal_id,
    body=body,
    sendUpdates="none",   # don't email attendees from a desktop app
).execute()
return event_from_google(resp)
```

### Write — update (with If-Match)

```python
try:
    resp = service.events().update(
        calendarId=cal_id,
        eventId=event.provider_event_id,
        body=event_to_google(event),
        sendUpdates="none",
        ifMatch=event.etag,
    ).execute()
except HttpError as e:
    if e.resp.status == 412:
        raise ConflictError()
    raise
```

### Rate limits

Default quota is 1 000 000 requests/day, 600/min/user. We don't worry
about hitting either at v0.1 — but the engine respects 429 / 403
`userRateLimitExceeded` by raising `TransientError` (which triggers
backoff).

---

## 7. Microsoft Graph backend

### OAuth setup

- **Application type**: Public client / native.
- **Tenant**: `common` (multi-tenant; works for personal + work
  accounts).
- **Scopes**:
  ```
  Calendars.ReadWrite
  offline_access
  User.Read
  ```
- **Loopback redirect**: `http://localhost` (Microsoft requires the
  literal string `localhost`, not `127.0.0.1`).

### Loopback flow

`msgraph-sdk-python`'s built-in `DeviceCodeCredential` is **not**
right for desktop — use `InteractiveBrowserCredential` from `azure-identity`:

```python
from azure.identity import InteractiveBrowserCredential

cred = InteractiveBrowserCredential(
    client_id=GRAPH_CLIENT_ID,
    tenant_id="common",
    redirect_uri="http://localhost",   # literal "localhost"
)
# Triggers browser flow on first .get_token() call
```

### Delta queries

```python
from msgraph.generated.users.item.calendars.item.calendar_view.delta.delta_request_builder import (
    DeltaRequestBuilder,
)

# Initial — calendarView/delta with a date window
url = (f"users/{me}/calendars/{cal_id}/calendarView/delta"
       f"?startDateTime={start.isoformat()}"
       f"&endDateTime={end.isoformat()}")

while url:
    resp = await graph_client.send(GET, url)
    for ev in resp["value"]:
        yield event_change_from_graph(ev)
    if "@odata.nextLink" in resp:
        url = resp["@odata.nextLink"]
    elif "@odata.deltaLink" in resp:
        save_cursor(resp["@odata.deltaLink"])
        url = None
```

The `deltaLink` URL embeds the date window. When the user navigates
beyond the cached window, **re-anchor**: drop the old cursor and
restart a fresh `calendarView/delta` for the new window.

### JSON → Event mapping

Similar shape to Google but field names differ. Selected:

| Graph field                  | `Event` attribute          |
| ---------------------------- | -------------------------- |
| `iCalUId`                    | `uid` (note casing)        |
| `id`                         | `provider_event_id`        |
| `subject`                    | `summary`                  |
| `bodyPreview`                | (display only; not stored) |
| `body.content`               | `description` (strip HTML if `body.contentType == "html"`) |
| `start.dateTime` + `start.timeZone` | `dtstart`, `tz`     |
| `end.dateTime`               | `dtend`                    |
| `isAllDay`                   | `all_day`                  |
| `recurrence`                 | construct `rrule`          |
| `location.displayName`       | `location`                 |
| `attendees`                  | `attendees`                |
| `responseStatus.response`    | per-attendee `partstat`    |
| `showAs`                     | `transparency`             |
| `sensitivity`                | (not stored v0.1)          |
| `reminderMinutesBeforeStart` | single `valarms` entry     |
| `@odata.etag`                | `etag`                     |
| `lastModifiedDateTime`       | `last_modified`            |

Graph's recurrence is structured (not RRULE) — build the RRULE
string from `recurrence.pattern.{type,interval,daysOfWeek,…}` plus
`recurrence.range.{type,endDate,numberOfOccurrences,…}`.
A helper `graph_pattern_to_rrule(pattern, range) -> str` belongs in
`backends/graph.py`.

---

## 8. `SyncEngine` — algorithm

```python
class SyncEngine(QObject):
    sync_started   = Signal(str)
    sync_finished  = Signal(str, int)
    sync_failed    = Signal(str, str)
    auth_expired   = Signal(str)
    conflict_detected = Signal(str)

    def __init__(self, store, secrets, factory) -> None:
        super().__init__()
        self._store, self._secrets, self._factory = store, secrets, factory
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

    async def _run_account(self, account: Account) -> None:
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
                delay = 300  # 5 min
            except CursorExpired as e:
                await self._full_resync(account, backend, e.calendar_id)
                delay = 5
            except AuthExpired:
                self.auth_expired.emit(account.id)
                return     # task ends; user must reconnect
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

        # 1) Drain pending writes
        for op in self._store.list_pending_ops(account.id):
            try:
                await self._apply_pending_op(backend, op)
                self._store.delete_pending_op(op.id)
            except ConflictError:
                self._store.mark_conflict(op.uid, op.calendar_id)
                self.conflict_detected.emit(op.uid)
            except TransientError:
                self._store.bump_pending_attempt(op.id)
                raise   # propagate to outer loop for backoff

        # 2) Pull incremental changes per calendar
        for cal in self._store.list_calendars(account.id, visible_only=False):
            cursor = SyncCursor.from_json(json.loads(cal.sync_cursor)) \
                     if cal.sync_cursor else None
            if cursor is None:
                async for changes, new_cur in backend.initial_sync(cal.provider_id):
                    self._store.apply_remote_changes(cal.id, changes, new_cur)
                    n_changes += len(changes)
            else:
                changes, new_cur = await backend.incremental_sync(
                    cal.provider_id, cursor
                )
                self._store.apply_remote_changes(cal.id, changes, new_cur)
                n_changes += len(changes)

        self.sync_finished.emit(account.id, n_changes)


def _next_backoff(prev: int) -> int:
    base = min(max(prev * 2, 5), 300)
    return int(base * random.uniform(0.5, 1.5))
```

### Apply-pending-op

```python
async def _apply_pending_op(self, backend, op: PendingOp) -> None:
    event = Event.from_json(json.loads(op.payload))
    if op.op == "create":
        canonical = await backend.create_event(op.calendar_id_provider, event)
        self._store.replace_event_with_canonical(op.uid, canonical)
    elif op.op == "update":
        canonical = await backend.update_event(
            op.calendar_id_provider, event, if_match=op.if_match
        )
        self._store.replace_event_with_canonical(op.uid, canonical)
    elif op.op == "delete":
        await backend.delete_event(
            op.calendar_id_provider, op.uid, if_match=op.if_match
        )
        self._store.confirm_deletion(op.uid, op.calendar_id)
```

### Conflict resolution (called from UI when user picks resolution)

```python
async def resolve_conflict(self, uid: str, calendar_id: str,
                           choice: Literal["local", "remote", "merge"],
                           merged: Event | None = None) -> None:
    if choice == "local":
        # Re-queue update with the server's current etag as If-Match
        latest_remote = await self._backend.fetch_one(calendar_id, uid)
        local = self._store.get_event(uid, calendar_id)
        self._store.queue_update(local, prev_etag=latest_remote.etag)
    elif choice == "remote":
        latest_remote = await self._backend.fetch_one(calendar_id, uid)
        self._store.upsert_remote(latest_remote)
    elif choice == "merge":
        assert merged is not None
        latest_remote = await self._backend.fetch_one(calendar_id, uid)
        self._store.queue_update(merged, prev_etag=latest_remote.etag)
    self._store.clear_conflict_state(uid, calendar_id)
```

---

## 9. `RecurrenceExpander`

```python
import recurring_ical_events
import icalendar

class RecurrenceExpander:
    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._cache: dict[tuple, list[Occurrence]] = {}  # LRU

    def expand_for_storage(self, event: Event,
                           window_start: datetime,
                           window_end: datetime,
                           ) -> list[Occurrence]:
        """Used by EventStore when rebuilding event_instances."""
        ical = self._event_to_vcalendar(event)
        occurrences = recurring_ical_events.of(ical).between(
            window_start, window_end
        )
        return [
            Occurrence(
                uid=event.uid,
                calendar_id=event.calendar_id,
                dtstart=occ["DTSTART"].dt,
                dtend=occ["DTEND"].dt,
                all_day=event.all_day,
                is_override=False,   # overrides come in as separate event rows
            )
            for occ in occurrences
        ]
```

`event_instances` only contains rows produced by master series.
**Override rows are stored as separate `events` rows with
`recurrence_id` set**, and they get a single `event_instances` row each.
The store's `apply_remote_changes` is responsible for *suppressing*
the auto-generated occurrence at that `recurrence_id` (so the user
sees only the override).

---

## 10. `NotificationScheduler`

```python
class NotificationScheduler:
    def __init__(self, store, recurrence) -> None:
        self._store, self._recurrence = store, recurrence
        self._heap: list[tuple[float, str, str]] = []  # (epoch, uid, calendar_id)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._rebuild_heap()
        self._task = asyncio.create_task(self._run())

    def _rebuild_heap(self) -> None:
        self._heap.clear()
        now = utc_now()
        end = now + timedelta(days=2)
        for inst in self._store.list_instances(now, end):
            event = self._store.get_event(inst.uid, inst.calendar_id)
            for alarm in event.valarms:
                fire = inst.dtstart + alarm.trigger_offset
                if fire > now:
                    heapq.heappush(self._heap,
                                   (fire.timestamp(), inst.uid, inst.calendar_id))

    async def _run(self) -> None:
        while True:
            if not self._heap:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                self._rebuild_heap()
                continue
            fire_at, uid, cal = self._heap[0]
            delay = max(fire_at - time.time(), 0)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                self._wake.clear()
                self._rebuild_heap()
            except asyncio.TimeoutError:
                heapq.heappop(self._heap)
                await self._fire(uid, cal)

    async def _fire(self, uid: str, cal: str) -> None:
        event = self._store.get_event(uid, cal)
        hide = Config.current().hide_notification_contents
        title = "Calendar reminder" if hide else event.summary
        body  = "" if hide else _fmt_time_range(event)
        await desktop_notifier.send(title=title, message=body,
                                    app_name="lilical")
```

Trigger `self._wake.set()` from `EventStore.events_changed` so the
heap rebuilds when events change.

---

## 11. UI scaffolding — `MainWindow`

```python
class MainWindow(QMainWindow):
    def __init__(self, *, config, event_store, sync_engine,
                 recurrence, secrets) -> None:
        super().__init__()
        self._cfg = config
        self._store = event_store
        self._sync = sync_engine

        self._setup_toolbar()
        self._setup_sidebar()
        self._setup_status_bar()
        self._setup_views()

        self._store.events_changed.connect(self._on_events_changed)
        self._sync.sync_started.connect(self._on_sync_started)
        self._sync.sync_finished.connect(self._on_sync_finished)
        self._sync.sync_failed.connect(self._on_sync_failed)
        self._sync.auth_expired.connect(self._on_auth_expired)
        self._sync.conflict_detected.connect(self._on_conflict)

        self._restore_geometry()

    def closeEvent(self, e):
        self._save_geometry()
        super().closeEvent(e)
```

The view-switch is a `QStackedWidget` with five children
(`MonthView`, `WeekView`, `DayView`, `YearView`, `AgendaView`).

---

## 12. The calendar grid (QGraphicsScene)

### Coordinate system (Week / Day)

Choose **scene units = pixels at default zoom**. Layout constants:

```python
TIME_AXIS_WIDTH = 60         # px
ALL_DAY_BAND_H  = 28         # px per row, expandable
PX_PER_HOUR     = 48         # default; 20–96 at min/max zoom
DAY_HEADER_H    = 32         # px
```

For a `WeekView` with `N` day columns:

```
scene_width  = TIME_AXIS_WIDTH + N * day_column_width
scene_height = DAY_HEADER_H + ALL_DAY_BAND_H + 24 * PX_PER_HOUR
day_column_width = (viewport_width - TIME_AXIS_WIDTH) / N
```

### Pixel ↔ datetime mapping

```python
def x_to_day_index(x: float) -> int:
    return int((x - TIME_AXIS_WIDTH) // day_column_width)

def y_to_minute_of_day(y: float) -> int:
    grid_y = y - DAY_HEADER_H - ALL_DAY_BAND_H
    return int(grid_y * 60 / PX_PER_HOUR)

def datetime_to_y(dt: datetime, day_start: datetime) -> float:
    minutes = (dt - day_start).total_seconds() / 60
    return DAY_HEADER_H + ALL_DAY_BAND_H + minutes * PX_PER_HOUR / 60
```

Snap-to-15-min during drag:

```python
def snap_minute(m: int, snap: int = 15) -> int:
    return round(m / snap) * snap
```

### Overlap-packing algorithm

When N events overlap in time within a single day, lay them out in
parallel columns. Standard "interval graph coloring" approach:

```python
def lay_out_day(events: list[EventInstance]) -> list[Placement]:
    events.sort(key=lambda e: (e.dtstart, e.dtend))
    columns: list[list[EventInstance]] = []  # one list per column
    for ev in events:
        for col in columns:
            if col[-1].dtend <= ev.dtstart:   # this column is free
                col.append(ev)
                break
        else:
            columns.append([ev])

    # Now assign each event a column index and total-columns at its time
    placements = []
    for ci, col in enumerate(columns):
        for ev in col:
            n_cols = _peak_cols_overlapping(ev, columns)
            placements.append(Placement(
                event=ev,
                column_index=ci,
                column_count=n_cols,
            ))
    return placements

def _peak_cols_overlapping(ev, columns) -> int:
    """Max number of columns with an overlap at any point in ev's range."""
    return sum(
        1 for col in columns
        if any(_overlaps(ev, other) for other in col)
    )
```

In Qt, place each `EventChip`:

```python
chip_x = TIME_AXIS_WIDTH + day_idx * day_w + (col_idx / col_total) * day_w
chip_w = day_w / col_total - CHIP_GAP_PX
chip_y = datetime_to_y(ev.dtstart, day_start)
chip_h = max(MIN_CHIP_H, datetime_to_y(ev.dtend, day_start) - chip_y)
```

### Month view layout

Simpler — fixed 6×7 grid. Each cell holds up to N "rows" of chips:
`rows_per_cell = (cell_height - DAY_NUM_H - PAD) // (CHIP_H + GAP)`.
Overflow row collapses to `▮N more`.

---

## 13. Drag-drop

```python
class EventChip(QGraphicsItem):
    def __init__(self, event: EventInstance, view: WeekView):
        super().__init__()
        self._event = event
        self._view  = view
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptHoverEvents(True)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = e.scenePos()
            self._drag_mode = self._hit_test_handle(e.pos())  # "top"|"move"|"bottom"
            self._ghost = self._spawn_ghost()
            e.accept()

    def mouseMoveEvent(self, e):
        if not hasattr(self, "_drag_anchor"):
            return
        delta = e.scenePos() - self._drag_anchor
        self._ghost.setPos(self.pos() + (
            QPointF(0, delta.y()) if self._drag_mode != "move"
            else QPointF(delta.x(), delta.y())
        ))
        self._view.show_drag_tooltip(self._compute_new_range(delta))

    def mouseReleaseEvent(self, e):
        if not hasattr(self, "_drag_anchor"):
            return
        new_range = self._compute_new_range(e.scenePos() - self._drag_anchor)
        self._view.commit_drag(self._event, self._drag_mode, new_range)
        self._cleanup_drag()
```

The `_view.commit_drag(...)` calls `EventStore.queue_update(...)` and
optimistically moves the chip; the eventual `events_changed` signal
just confirms.

---

## 14. Event dialog wiring

```python
class EventDialog(QDialog):
    def __init__(self, *, parent, event: Event | None,
                 default_calendar: str,
                 store: EventStore) -> None:
        super().__init__(parent)
        self._store = store
        self._editing = event is not None
        self._original = event
        self._build_ui(event, default_calendar)

    def accept(self) -> None:
        try:
            new_event = self._collect_from_form()
        except FormError as e:
            self._show_inline_error(e)
            return

        if self._editing and self._original.rrule:
            scope = self._ask_recurring_scope()   # "this" | "future" | "all"
            new_event = _apply_scope(self._original, new_event, scope)

        if self._editing:
            self._store.queue_update(new_event, prev_etag=self._original.etag)
        else:
            self._store.queue_create(new_event)
        super().accept()
```

Recurring-scope helper:

```python
def _apply_scope(original: Event, edited: Event,
                 scope: Literal["this","future","all"]) -> Event | tuple[Event, Event]:
    if scope == "all":
        return edited
    if scope == "this":
        # Single-instance override: same uid, set recurrence_id
        return dataclasses.replace(
            edited,
            recurrence_id=original.dtstart,
            rrule=None,
        )
    if scope == "future":
        # Truncate original with UNTIL=<recurrence_id - 1day>; new series for edited
        truncated = dataclasses.replace(
            original,
            rrule=_set_until(original.rrule, original.dtstart - timedelta(seconds=1)),
        )
        new_series = dataclasses.replace(
            edited,
            uid=str(uuid.uuid4()),
            recurrence_id=None,
            sequence=0,
        )
        return (truncated, new_series)
```

---

## 15. Conflict dialog wiring

When `SyncEngine.conflict_detected.emit(uid)` fires:

```python
def _on_conflict(self, uid: str) -> None:
    local = self._store.get_event(uid)
    remote = self._store.get_remote_snapshot(uid)   # stored alongside in conflict_state path
    dlg = ConflictDialog(self, local=local, remote=remote)
    if dlg.exec() == QDialog.Accepted:
        choice = dlg.choice           # "local"|"remote"|"merge"
        merged = dlg.merged_event     # only if choice=="merge"
        asyncio.ensure_future(
            self._sync.resolve_conflict(uid, local.calendar_id, choice, merged)
        )
```

The store needs a way to keep both the dirty local row *and* the
remote version when a conflict is detected. Add a `conflict_remote`
table or — simpler — store the remote snapshot in
`events.conflict_remote_json TEXT` and clear it on resolution.

---

## 16. Flatpak manifest

```yaml
# flatpak/io.github.lilical.Lilical.yml
app-id: io.github.lilical.Lilical
runtime: org.kde.Platform
runtime-version: '6.7'
sdk: org.kde.Sdk
command: lilical

finish-args:
  - --share=ipc
  - --share=network
  - --socket=fallback-x11
  - --socket=wayland
  - --device=dri
  - --talk-name=org.freedesktop.Notifications
  - --talk-name=org.freedesktop.secrets
  - --filesystem=xdg-data/lilical
  - --filesystem=xdg-config/lilical
  - --filesystem=xdg-cache/lilical

modules:
  - python3-modules.yml         # generated by req2flatpak
  - name: lilical
    buildsystem: simple
    build-commands:
      - pip3 install --prefix=${FLATPAK_DEST} .
      - install -Dm644 data/io.github.lilical.Lilical.desktop
            ${FLATPAK_DEST}/share/applications/io.github.lilical.Lilical.desktop
      - install -Dm644 data/io.github.lilical.Lilical.metainfo.xml
            ${FLATPAK_DEST}/share/metainfo/io.github.lilical.Lilical.metainfo.xml
    sources:
      - type: dir
        path: ..
```

`.desktop` file:

```
[Desktop Entry]
Type=Application
Name=lilical
Comment=Calendar for Google, Outlook, and CalDAV
Exec=lilical %f
Icon=io.github.lilical.Lilical
Terminal=false
Categories=Office;Calendar;
MimeType=text/calendar;
StartupWMClass=lilical
```

---

## 17. Test catalog

### 17.1 Unit tests (no I/O)

| File                                  | Tests                                                     |
| ------------------------------------- | --------------------------------------------------------- |
| `tests/unit/recurrence_test.py`       | Daily/weekly/monthly RRULE; EXDATE; RDATE; RECURRENCE-ID override; UNTIL truncation; COUNT; DST transition |
| `tests/unit/datetime_test.py`         | Aware-only invariant; all-day round-trip; tz conversion   |
| `tests/unit/google_mapping_test.py`   | Each Google JSON shape (timed, all-day, recurring, recurring with override, cancelled) → Event and back |
| `tests/unit/graph_mapping_test.py`    | Same for Graph                                            |
| `tests/unit/ical_mapping_test.py`     | Same for VEVENT (using ICS fixtures in §17.4)             |
| `tests/unit/event_layout_test.py`     | Overlap packing: 1 event, 2 sequential, 2 overlap, 3 stack, 4 with gaps |
| `tests/unit/conflict_test.py`         | SEQUENCE tiebreaker; LAST-MODIFIED tiebreaker; merge      |
| `tests/unit/backoff_test.py`          | Backoff series + jitter bounds                            |
| `tests/unit/pending_op_test.py`       | Order preservation; retry counter; etag tracking          |
| `tests/unit/secrets_test.py`          | Encrypted-file roundtrip; wrong passphrase rejected       |
| `tests/unit/instances_rebuild_test.py`| Window shift; single-event rebuild; override suppression  |

### 17.2 Qt UI tests (`pytest-qt`)

| File                                 | Tests                                                                 |
| ------------------------------------ | --------------------------------------------------------------------- |
| `tests/ui/main_window_test.py`       | Switching views; clicking today; keyboard shortcuts                   |
| `tests/ui/event_dialog_test.py`      | Create / edit / cancel; recurring-scope dialog                        |
| `tests/ui/drag_drop_test.py`         | Drag horizontal (day change), vertical (time change), top edge (resize); Esc cancels |
| `tests/ui/conflict_dialog_test.py`   | Local / remote / merge paths                                          |
| `tests/ui/quick_add_test.py`         | "Lunch tomorrow at 1pm" → preview shows correct event                 |

### 17.3 Integration tests

| File                                       | Setup                                            |
| ------------------------------------------ | ------------------------------------------------ |
| `tests/integration/caldav_radicale_test.py`| `pixi run radicale` (Docker) → real round-trips  |
| `tests/integration/google_vcr_test.py`     | Replay recorded cassettes via `pytest-recording` |
| `tests/integration/graph_vcr_test.py`      | Same                                             |
| `tests/integration/sync_engine_test.py`    | Fake `Backend` driving the engine through every state-machine transition |

### 17.4 Fixture catalog

`tests/fixtures/ics/`:

- `simple_timed.ics` — one VEVENT, timed, no recurrence
- `simple_all_day.ics` — `DTSTART;VALUE=DATE:…`
- `weekly_rrule.ics` — RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
- `monthly_rrule.ics` — RRULE:FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR
- `with_exdate.ics`
- `with_rdate.ics`
- `with_override.ics` — master + RECURRENCE-ID
- `with_dst_transition.ics`
- `with_valarm.ics`
- `with_attendees.ics`
- `apple_style_override.ics` — Apple Calendar's wire format
- `nextcloud_export.ics` — full Nextcloud export of a small calendar
- `large_corpus.ics` — ~500 events for perf tests

`tests/fixtures/google/`:

- `events_list_initial.json` — first page of 250 events
- `events_list_initial_page2.json` — second page with nextPageToken
- `events_list_final.json` — last page with nextSyncToken
- `event_recurring.json`
- `event_recurring_override.json`
- `event_cancelled.json`
- `events_list_410.json` — body of a 410 GONE response
- `events_list_after_token.json` — incremental with new event
- `oauth_token_response.json`

`tests/fixtures/graph/`:

- Same shape: `calendar_view_delta_initial.json`,
  `calendar_view_delta_next.json`, `event_recurring.json`,
  `oauth_token_response.json`.

`tests/fixtures/caldav/`:

- `propfind_principal.xml`
- `propfind_home_set.xml`
- `propfind_calendars.xml`
- `report_sync_collection.xml`
- `put_event_response.xml`

---

## 18. Implementation order — checklist

Implement in this order. Each step is independently testable.

### M0 — Scaffolding (1 d)

- [ ] `pixi.toml`, `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`
- [ ] `src/lilical/__main__.py` + `app.py` boots an empty `QMainWindow`
- [ ] `migrations/env.py` + initial revision (§2)
- [ ] `pixi run migrate` works
- [ ] `pixi run test` runs (empty suite passes)
- [ ] CI: GitHub Actions matrix `pixi install && pixi run test`

### M1 — CalDAV end-to-end (5 d)

- [ ] `models/*.py` SQLAlchemy declarations (§2)
- [ ] `storage/event_store.py` queries + mutations (§3)
- [ ] `storage/secrets.py` keyring + encrypted-file fallback
- [ ] `backends/base.py` Protocol + exceptions (§4)
- [ ] `backends/caldav.py` discovery + sync + CRUD + Radicale shim (§5)
- [ ] `recurrence/expander.py` (§9)
- [ ] `ui/main_window.py` skeleton (§11)
- [ ] `ui/views/month.py` + `ui/views/week.py` (§12)
- [ ] `ui/widgets/event_chip.py` + drag-drop (§13)
- [ ] `ui/widgets/event_dialog.py` (§14)
- [ ] Round-trip create/edit/delete in Radicale verified by §17.3

### M2 — Google (7 d)

- [ ] `backends/google.py` (§6) — JSON mapping, sync, CRUD
- [ ] `ui/widgets/account_setup.py` Google branch
- [ ] VCR cassettes (§17.4)
- [ ] Round-trip verified against real Google account

### M3 — Microsoft Graph (7 d)

- [ ] `backends/graph.py` (§7) — JSON mapping (incl. recurrence
      pattern → RRULE), sync, CRUD
- [ ] `ui/widgets/account_setup.py` Microsoft branch
- [ ] VCR cassettes (§17.4)
- [ ] Round-trip verified against real Microsoft account

### M4 — Remaining views (5 d)

- [ ] `ui/views/day.py` (reuse Week with day_count=1)
- [ ] `ui/views/year.py` (mini-month grid + density tint)
- [ ] `ui/views/agenda.py` (virtualized list with `QAbstractItemView`)
- [ ] `ui/widgets/mini_month.py` (sidebar)
- [ ] Day-count slider in toolbar (Week view)
- [ ] Keyboard shortcuts (UI-SPEC §12)

### M5 — Conflicts + write polish (3 d)

- [ ] `sync/conflicts.py` 3-way merge primitives
- [ ] `ui/widgets/conflict_dialog.py` (§15)
- [ ] `SyncEngine.resolve_conflict` (§8)
- [ ] Per-account error pill in status bar
- [ ] Retry-with-backoff verified by §17.1

### M6 — System integration (2 d)

- [ ] `ui/tray.py` `QSystemTrayIcon` with quick-add menu
- [ ] `ui/widgets/quick_add.py` natural-language input (§11 of UI-SPEC)
- [ ] `ui/notifications.py` `NotificationScheduler` (§10)
- [ ] `.ics` file association — `MimeType=text/calendar` in `.desktop`
- [ ] `ics/importer.py` — `icalendar` parse + preview dialog

### M7 — Packaging (3 d)

- [ ] `flatpak/io.github.lilical.Lilical.yml` (§16)
- [ ] `data/*.metainfo.xml` AppStream metadata
- [ ] App icons (scalable SVG + 256×256 PNG)
- [ ] Flathub submission

---

## 19. Things to NOT implement in v0.1 (resist the urge)

These will tempt you mid-implementation. Don't.

- **A plugin system** for backends. The Protocol is enough; v0.2 if
  ever.
- **Full-text search.** FTS5 in v0.2. v0.1 search shortcut is just
  a placeholder.
- **CalDAV scheduling extensions** (RFC 6638 — auto-invitations).
  Wire format only.
- **iCalendar TIMEZONE blocks** generation. Rely on `tz` IANA name +
  `icalendar` library's defaults.
- **A custom thread pool.** Use `asyncio.to_thread`.
- **Per-event color picker beyond a fixed palette.** Pick from 12
  swatches; "custom" is v0.2.
- **OAuth scopes beyond Calendar/Calendars.ReadWrite.** No contacts,
  no mail.
- **Calendar creation/deletion via the API.** v0.1 only reads the
  list and toggles visibility.
- **Free-busy lookup.**
- **A settings dialog with 40 toggles.** Ship five: theme, font size,
  week starts on, default view, reduce motion.
