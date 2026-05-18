# lilical — Linux Calendar App

A multi-backend calendar for the Linux desktop. Read AND write across
**Google Calendar**, **Outlook/Microsoft 365**, and **CalDAV**, in one app,
with a dense Business-Calendar-inspired UI.

### Doc set

| Doc                                    | Purpose                                          |
| -------------------------------------- | ------------------------------------------------ |
| [PLAN.md](PLAN.md) (this file)         | What we're building, why, milestones, risks      |
| [ARCHITECTURE.md](ARCHITECTURE.md)     | Layers, `Backend` protocol, async/threading      |
| [DATA-MODEL.md](DATA-MODEL.md)         | SQLite schema, in-memory `Event`, migrations     |
| [UI-SPEC.md](UI-SPEC.md)               | Visual design, views, dialogs, shortcuts         |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Concrete code patterns, algorithms, test catalog, build order — read this when you're writing code |

---

## 1. Context & goals

### Why this project

GNOME Calendar and KOrganizer are mature but limited: GNOME Calendar only
talks to backends via Evolution-Data-Server (which does Google/Exchange but
in a quirky way), and KOrganizer's UX hasn't kept pace with modern mobile
calendars. Karlender (Rust+GTK4) is the closest Rust/Linux reference but
**CalDAV-only**. No Linux-native app today does *first-class* Google +
Outlook + CalDAV with a dense desktop UI.

### Goals

1. **One app, three backends.** Native Google Calendar, Microsoft Graph
   (Outlook/365), and CalDAV, with read AND write support and a uniform UX.
2. **Business-Calendar-class density.** Month/Week/Day/Year/Agenda views
   with drag-to-move/resize/create, color-coded events, mini-month picker.
3. **Linux-first.** Polished on GNOME and KDE, packaged for Flathub.
4. **Offline-first.** SQLite is the source of truth; the app remains useful
   without network and reconciles when sync resumes.

### Non-goals (v0.1)

- iOS/macOS/Windows ports (Qt keeps them plausible, but not targeted now).
- Tasks integration, attachments, weather, contacts/birthdays, free-busy
  lookup, RSVP/invitations, shared-calendar delegation. All deferred to v0.2+.
- A separate sync daemon. In-process worker is sufficient at v0.1.

---

## 2. Stack (locked)

| Concern             | Pick                                                            |
| ------------------- | --------------------------------------------------------------- |
| Language            | Python 3.12+                                                    |
| Env / packaging     | **pixi** (conda-forge + PyPI) for development                   |
| GUI toolkit         | **PySide6 6.11+** (LGPL, official Qt)                           |
| Async integration   | **qasync 0.28+** (asyncio + Qt event loop)                      |
| Calendar views      | **QGraphicsView/QGraphicsScene** for grids; QWidgets for chrome |
| Theming             | QSS (Qt stylesheets), Material-ish dark/light                   |
| Google Calendar     | `google-api-python-client` + `google-auth-oauthlib` (PKCE+loopback) |
| Microsoft Graph     | `msgraph-sdk-python` (official, active)                         |
| CalDAV              | `caldav` (latest stable, with per-server quirk shims)           |
| iCalendar parse     | `icalendar` (latest stable)                                     |
| RRULE expansion     | `recurring-ical-events` 2.2+ (3.x once released)                |
| Timezones           | stdlib `zoneinfo` + `tzdata` (Flatpak/Windows portability)      |
| Local storage       | SQLite (WAL) via **SQLAlchemy 2.x** + Alembic                   |
| Secret storage      | `keyring` (SecretService) + encrypted-file fallback             |
| Notifications       | `desktop-notifier` (async D-Bus)                                |
| System tray         | `QSystemTrayIcon`                                               |
| Test framework      | `pytest` + `pytest-qt` + `pytest-asyncio`                       |
| Lint / type         | `ruff` + `basedpyright`                                         |
| App distribution    | **Flatpak / Flathub** primary; AppImage fallback                |

**Rejected:** Rust (slower to ship), GTK4+libadwaita (user prefers Qt),
egui/Iced/Slint, Qt Quick (less prior art for dense calendar grids in
Python), Tauri, Poetry/PDM/venv (pixi handles native deps better).

---

## 3. Environment management — pixi

Pixi gives us:

- A single, lockfile-driven dev environment that handles **Qt's native
  libraries** cleanly from conda-forge (no system Qt dependency hell, no
  wheel-mismatch on PySide6).
- Cross-distro reproducibility (`pixi install` works the same on Arch,
  Fedora, Ubuntu).
- PyPI fallback for the handful of deps not on conda-forge.
- Pixi **tasks** replace Makefiles for `dev`, `test`, `lint`, `migrate`,
  `build`, etc.
- Multiple **features/environments** (dev, test, flatpak-build) without
  juggling extras.

### `pixi.toml` shape (sketch, not committed)

```toml
[project]
name = "lilical"
version = "0.1.0"
description = "Linux calendar app — Google, Outlook, CalDAV"
channels = ["conda-forge"]
platforms = ["linux-64", "linux-aarch64"]

[dependencies]
python = "3.12.*"
pyside6 = ">=6.11"
sqlalchemy = ">=2.0"
alembic = "*"
google-api-python-client = "*"
google-auth-oauthlib = "*"
caldav = ">=1.3"
icalendar = ">=5.0"
keyring = "*"
tzdata = "*"
dateparser = "*"

[pypi-dependencies]
qasync = ">=0.28"
msgraph-sdk = "*"
recurring-ical-events = ">=2.2"   # 3.x once released and stable
desktop-notifier = "*"

[feature.dev.dependencies]
ruff = "*"
basedpyright = "*"
pytest = "*"
pytest-qt = "*"
pytest-asyncio = "*"
pytest-cov = "*"

[feature.dev.pypi-dependencies]
pytest-recording = "*"   # VCR-style HTTP record/replay for backend tests

[environments]
default = { features = [], solve-group = "main" }
dev = { features = ["dev"], solve-group = "main" }

[tasks]
run        = "python -m lilical"
test       = "pytest -v"
test-cov   = "pytest --cov=lilical --cov-report=term-missing"
lint       = "ruff check src tests"
fmt        = "ruff format src tests"
typecheck  = "basedpyright src"
migrate    = "alembic upgrade head"
makemig    = { cmd = "alembic revision --autogenerate -m", depends-on = [] }
radicale   = "docker run --rm -p 5232:5232 tomsquest/docker-radicale"
flatpak-build = "flatpak-builder --user --install --force-clean build-dir flatpak/org.lilical.Lilical.yml"
```

### Onboarding

```bash
git clone <repo> && cd lilical
pixi install              # solves & installs all deps
pixi run migrate          # initial DB schema
pixi run run              # launch the app
pixi run test             # full suite
```

No `pip`, no `venv`, no `poetry`. Contributors install pixi once
(`curl -fsSL https://pixi.sh/install.sh | bash`) and that's it.

---

## 4. Architecture

Three layers, with a **`Backend` protocol** abstracting Google/Graph/CalDAV
behind one interface so the UI never special-cases providers.

```
┌─────────────────────────── UI ───────────────────────────┐
│  PySide6 widgets + QGraphicsScene calendar views         │
│  (Month / Week / Day / Year / Agenda + sidebar)          │
└──────────────────────────────────────────────────────────┘
                       │ Qt signals/slots
┌────────────────────── Core ──────────────────────────────┐
│  EventStore      (SQLAlchemy + SQLite WAL)               │
│  SyncEngine      (qasync tasks, per-account schedule)    │
│  RecurrenceExpander  (recurring-ical-events)             │
│  ConflictResolver    (etag/SEQUENCE/LAST-MODIFIED)       │
│  NotificationScheduler                                   │
│  ColorPalette                                            │
└──────────────────────────────────────────────────────────┘
                       │ Backend protocol
┌─────────────────── Backends ─────────────────────────────┐
│  GoogleBackend   (google-api-python-client, syncToken)   │
│  GraphBackend    (msgraph-sdk-python, delta queries)     │
│  CalDavBackend   (caldav, sync-collection + ETag)        │
└──────────────────────────────────────────────────────────┘
                       │
                  OAuth / DAV / HTTPS
```

### 4.1 `Backend` protocol

A single interface that abstracts Google / Graph / CalDAV. Methods cover
calendar listing, initial + incremental sync (cursor-driven), and
CRUD on events with optimistic-concurrency `If-Match`.

The authoritative signature lives in [ARCHITECTURE.md §2](ARCHITECTURE.md#2-the-backend-protocol).
Don't duplicate it here; if you're editing this section, update
ARCHITECTURE.md instead.

`SyncCursor` is an opaque per-backend token:

- **Google** → `syncToken` (opaque, expires after ~30 days idle → 410 GONE)
- **Graph** → `@odata.deltaLink` URL (per calendarView window)
- **CalDAV** → `sync-token` (RFC 6578) with CTAG/ETag fallback for older
  servers (Apple-style)

### 4.2 Internal event model (JSCalendar-shaped)

Storing a normalized `Event` lets the UI render once regardless of provider.
Provider-specific serialization happens at the backend boundary.

| Field                | Type                | Notes                                          |
| -------------------- | ------------------- | ---------------------------------------------- |
| `uid`                | str (part of PK)    | RFC 5545 UID; full PK is composite — see [DATA-MODEL §3.3](DATA-MODEL.md#33-events) |
| `account_id`         | str (FK)            |                                                |
| `calendar_id`        | str (FK)            |                                                |
| `dtstart`            | datetime + tz       | UTC stored; tz held separately                 |
| `dtend`              | datetime + tz       |                                                |
| `tz`                 | str (IANA)          | preserved for DST-correct recurrence           |
| `all_day`            | bool                |                                                |
| `summary`            | str                 |                                                |
| `description`        | str                 |                                                |
| `location`           | str                 |                                                |
| `recurrence_rule`    | str (RRULE)         |                                                |
| `recurrence_id`      | datetime (nullable) | for instance overrides                         |
| `exdates`            | JSON list           | EXDATE entries                                 |
| `rdates`             | JSON list           | RDATE entries                                  |
| `attendees`          | JSON                | list of {email, name, role, partstat}          |
| `categories`         | JSON list           |                                                |
| `color`              | str (hex/index)     |                                                |
| `status`             | enum                | CONFIRMED/TENTATIVE/CANCELLED                  |
| `etag`               | str                 | server etag (or sequence-derived for Google)   |
| `sequence`           | int                 | iCal SEQUENCE                                  |
| `last_modified`      | datetime            |                                                |
| `local_dirty`        | bool                | unsynced local edit                            |
| `deleted_locally`    | bool                | tombstone awaiting server confirmation         |
| `conflict_state`     | enum (nullable)     | NONE / SERVER_WINS / LOCAL_WINS / NEEDS_USER   |

A separate `event_instances` materialized view holds **expanded recurring
occurrences** for fast range queries (e.g. "give me everything between
2026-05-01 and 2026-05-31"). It's rebuilt for an event whenever the event,
RRULE, EXDATE, or RDATE changes. Bounded window (e.g. ±2 years from today)
to keep the table small.

### 4.3 Sync strategy

- **Provider-native incremental sync.** Use the token each provider offers;
  never poll the full collection on a normal sync.
- **Token expiry.** Google's `syncToken` returns 410 GONE eventually; Graph
  rotates `deltaLink`; CalDAV `sync-token` can become invalid. In each
  case, fall back to a one-shot full re-sync of that one calendar only.
- **Per-account scheduling.** Each account runs its own `asyncio.Task` in
  the qasync loop, with a default 5-minute poll and an immediate-refresh
  signal triggered by user action.
- **Write queue.** Local edits set `local_dirty=True` and append to a
  `pending_ops` table. The sync engine drains pending ops before reading
  the next batch of remote changes.
- **Conflict resolution.** When the same event was modified on both sides
  since the last sync:
  - For low-risk fields (`description`, `location`, `categories`) → server
    wins silently.
  - For load-bearing fields (`summary`, `dtstart`, `dtend`, `attendees`,
    `rrule`) → surface a conflict dialog with side-by-side diff. User picks
    *local*, *remote*, or *merge*.
  - Tiebreakers: iCalendar `SEQUENCE` (higher wins); ties broken by
    `LAST-MODIFIED`.

### 4.4 Auth

| Backend  | Flow                                                           |
| -------- | -------------------------------------------------------------- |
| Google   | OAuth 2.0 **PKCE + loopback** (127.0.0.1:random) via `google-auth-oauthlib` |
| Graph    | OAuth 2.0 **PKCE + loopback** via `msgraph-sdk-python` (Microsoft Identity Platform) |
| CalDAV   | HTTP Basic or App-Password; URL discovery via `.well-known/caldav` |

Refresh tokens / app passwords go into the OS keyring via the `keyring`
library. If no SecretService is reachable (headless, AppImage on a system
without gnome-keyring), the app automatically falls back to an encrypted file
at `$XDG_DATA_HOME/lilical/credentials.enc` (AES-GCM, key derived via
HKDF-SHA256 from `/etc/machine-id` with a per-app info string — no key file
on disk, no passphrase prompt). A one-time WARNING is logged at startup when
the fallback is active. Note: the fallback file is machine-bound; moving it
to another host without its originating `/etc/machine-id` renders it
unreadable.

### 4.5 Recurrence

`recurring-ical-events` is the only widely-used Python library that
handles **all** the corner cases we care about:

- `RRULE` with `BYDAY`, `BYMONTHDAY`, `BYSETPOS`, `COUNT`/`UNTIL`
- `EXDATE` exclusions
- `RDATE` additions
- `RECURRENCE-ID` instance overrides (the "edited single occurrence" case)
- DTSTART vs DTEND duration preservation across DST

`RecurrenceExpander` wraps it with caching: an LRU keyed by
`(uid, etag, range_start, range_end)` so the same week view doesn't
re-expand every paint.

### 4.6 Notifications

`desktop-notifier` (async D-Bus) for upcoming-event reminders. Reminders
honor each event's iCalendar `VALARM` blocks where present, otherwise fall
back to user-configured defaults (e.g. 10 min before). `NotificationScheduler`
maintains a min-heap of upcoming triggers and a single `asyncio.Task` that
sleeps to the next trigger.

**Privacy.** Event titles can be sensitive. Preferences have a
**Hide notification contents** toggle (off by default) — when on, every
reminder is delivered as a generic *"Calendar reminder — open lilical"*
with no title, location, or attendee data. We don't try to detect
screen-lock state ourselves (no portable Qt API); the user's notification
daemon already handles lock-screen redaction per its own rules. We set
`urgency=normal` and the `category=im.received`-style hint so KDE /
GNOME treat reminders consistently with other apps.

### 4.7 Threading model

Single Python process, single qasync event loop. All I/O is `async def`.
CPU-bound work (RRULE expansion of huge series, ICS imports) runs in
`asyncio.to_thread()` to avoid blocking the UI. No `QThread` / no manual
thread management.

---

## 5. UI design

### 5.1 View grammar

| View    | Layout                                | Range          | Drag verbs                  |
| ------- | ------------------------------------- | -------------- | --------------------------- |
| Month   | 6×7 grid of day cells                 | calendar month | move event between days     |
| Week    | configurable 1–14 columns + time axis | N days         | move, resize edge, create   |
| Day     | single column, dense time grid        | 1 day          | move, resize, create        |
| Year    | 12 mini-months, heatmap-shaded        | 12 months      | click-to-jump (no drag)     |
| Agenda  | virtualized chronological list        | unbounded      | multi-select for batch ops  |

### 5.2 Chrome

- **Top toolbar:** view switcher, week-span slider (Week view only),
  today/forward/back, search (v0.2 deferred), account-status pill, settings.
- **Left sidebar:** mini-month picker, account list, per-calendar visibility
  toggles, color swatches.
- **Bottom status bar:** sync state, "last synced X ago", quick-error pill.
- **Tray icon:** show/hide window, "quick add" popover.

### 5.3 Event chip rendering

`EventChip` is a `QGraphicsItem` drawn with QPainter directly — gives us
pixel control needed for Business-Calendar-style chips (rounded corners,
gradient by category, "fade right" for overflow text, drag handles on
edges in week/day views).

### 5.4 Theming

QSS stylesheets in `src/lilical/ui/styles/`. Two built-in themes (light,
dark) plus accent-color override. We follow KDE's Breeze and GNOME's
Adwaita color tokens roughly so the app feels at home on either desktop.

### 5.5 Accessibility

Qt's accessibility APIs are mature on Linux (AT-SPI via QAccessible). We
ensure every interactive item has `setAccessibleName` and
`setAccessibleDescription`. Keyboard navigation: arrow keys move the
selected day; `n` new event, `j`/`k` next/prev event, `e` edit, `Delete`
delete. Screen-reader testing with Orca before v0.1 ships.

---

## 6. v0.1 MVP scope (locked)

**In scope:**

- Read AND write across Google, Graph, CalDAV
- Multi-account per backend
- Month, Week (1–14 days), Day, Year, Agenda views
- Mini-month picker
- OAuth wizard (Google + Graph), CalDAV account setup
- RRULE expansion with EXDATE/RDATE/RECURRENCE-ID
- Conflict resolution UI
- Desktop notifications scheduled from VALARM
- System tray with show/hide + quick-add
- Offline-first cache with write queue
- `.ics` file association ("preview & import")
- Flatpak package on Flathub

**Out of scope (v0.2+):**

- Tasks, attachments, weather, contacts/birthdays
- Free-busy lookup, RSVP/invitations
- Shared calendar delegation
- Full-text search across events
- KDE/GNOME shell calendar-widget integration
- Theme editor

---

## 7. Project layout

```
lilical/
├── pixi.toml                       # env, tasks, deps
├── pixi.lock                       # committed lockfile
├── pyproject.toml                  # PEP 621 (build), tool config
├── README.md
├── LICENSE                         # GPL-3.0
├── PLAN.md                         # this file
├── flatpak/
│   └── org.lilical.Lilical.yml
├── data/
│   ├── org.lilical.Lilical.desktop
│   ├── org.lilical.Lilical.metainfo.xml
│   └── icons/
│       ├── scalable/apps/org.lilical.Lilical.svg
│       └── 256x256/apps/org.lilical.Lilical.png
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/
├── src/lilical/
│   ├── __main__.py                 # entry point
│   ├── app.py                      # QApplication + qasync boot
│   ├── config.py                   # XDG paths, QSettings wrapper
│   ├── logging_setup.py
│   ├── models/
│   │   ├── db.py                   # SQLAlchemy base
│   │   ├── event.py
│   │   ├── calendar.py
│   │   └── account.py
│   ├── storage/
│   │   ├── event_store.py
│   │   ├── pending_ops.py
│   │   └── secrets.py
│   ├── backends/
│   │   ├── base.py                 # Backend Protocol
│   │   ├── google.py
│   │   ├── graph.py
│   │   └── caldav.py               # + per-server shims
│   ├── sync/
│   │   ├── engine.py
│   │   ├── cursor.py
│   │   └── conflicts.py
│   ├── recurrence/
│   │   └── expander.py
│   ├── ui/
│   │   ├── main_window.py
│   │   ├── sidebar.py
│   │   ├── views/
│   │   │   ├── month.py
│   │   │   ├── week.py             # 1–14 day span
│   │   │   ├── day.py
│   │   │   ├── year.py
│   │   │   └── agenda.py
│   │   ├── widgets/
│   │   │   ├── mini_month.py
│   │   │   ├── event_chip.py       # QGraphicsItem
│   │   │   ├── event_dialog.py
│   │   │   ├── account_setup.py    # OAuth wizard
│   │   │   └── conflict_dialog.py
│   │   ├── tray.py
│   │   ├── notifications.py
│   │   └── styles/
│   │       ├── light.qss
│   │       └── dark.qss
│   └── ics/
│       └── importer.py
└── tests/
    ├── unit/
    ├── integration/
    │   ├── caldav_radicale_test.py
    │   ├── google_vcr_test.py
    │   └── graph_vcr_test.py
    └── fixtures/
```

---

## 8. Milestones

| #  | Milestone                       | Outcome                                                          | Est.    |
| -- | ------------------------------- | ---------------------------------------------------------------- | ------- |
| M0 | Scaffolding                     | `pixi run run` boots a blank window with logging + DB migration  | 1 day   |
| M1 | CalDAV end-to-end               | Month+Week views talking to Radicale, drag-edit round-trips      | 3–5 d   |
| M2 | Google Calendar                 | OAuth wizard + `syncToken` incremental sync + write              | 5–7 d   |
| M3 | Microsoft Graph                 | Same wizard branch + `deltaLink` + write                         | 5–7 d   |
| M4 | Day, Year, Agenda views         | Five views complete; mini-month sidebar; view switcher           | 3–5 d   |
| M5 | Conflict resolution + write polish | 3-way merge UI, retry queue, dirty-state reconciliation       | 2–3 d   |
| M6 | System integration              | Tray, notifications from VALARM, `.ics` file association         | 2 d     |
| M7 | Packaging                       | Flatpak on Flathub, AppImage fallback                            | 2–3 d   |

**Total v0.1: ~5–7 weeks of focused work** (sums to 23–34 person-days
across the milestones above). M2 and M3 each budget for OAuth-wizard
UI + loopback handler + token persistence + initial sync + write
round-trips — provider integrations are heavier than they look once
you account for edge cases.

CalDAV is M1 because (a) no OAuth dance to debug first, (b) `caldav` is the
most batteries-included of the three, (c) easy to test against a local
Radicale Docker container.

---

## 9. Testing strategy

| Layer            | Tooling                                                | Notes                                                |
| ---------------- | ------------------------------------------------------ | ---------------------------------------------------- |
| Unit             | `pytest`                                               | Pure logic: recurrence, conflict tiebreakers, serializers |
| Qt UI            | `pytest-qt`                                            | View interactions, drag→event-update wiring          |
| Async            | `pytest-asyncio`                                       | Sync engine, scheduler                               |
| CalDAV           | Real Radicale in Docker via `pixi run radicale`        | Reusable across dev + CI                             |
| Google / Graph   | `pytest-recording` (VCR) cassettes                     | Real one-time recording, replay in CI                |
| End-to-end       | Manual via the smoke script in §11                     | Pre-release checklist                                |

CI: GitHub Actions matrix on `linux-64`, `pixi install && pixi run test`.

---

## 10. Risks & mitigations

| Risk                                                  | Mitigation                                                          |
| ----------------------------------------------------- | ------------------------------------------------------------------- |
| `caldav` write quirks across servers                  | Per-server shims (Nextcloud, iCloud, Fastmail, Google CalDAV);      |
|                                                       | integration matrix against Radicale + Nextcloud                     |
| Google `syncToken` 410 GONE on long idle              | Detect 410; full re-sync of that calendar only                      |
| Graph `deltaLink` window limits                       | Window per ±90 days; re-anchor when crossing boundary               |
| RRULE edge cases (Apple-style overrides, EXRULE)      | `recurring-ical-events` covers most; fall back to full expansion;   |
|                                                       | extensive fixture corpus from RFC 5545 + real-world ICS files       |
| qasync rough edges on shutdown                        | Explicit `loop.close()` in `aboutToQuit`; tested by Maestral        |
| Flatpak SecretService access                          | Manifest declares `--talk-name=org.freedesktop.secrets`             |
| Headless / no keyring                                 | Encrypted-file fallback in `$XDG_DATA_HOME/lilical/credentials.enc` |
| QSystemTrayIcon absent on GNOME                       | `isSystemTrayAvailable()` check; degrade gracefully (no tray)       |
| Qt LGPL distribution                                  | Confirmed compatible with Flathub + GPL-3 app                       |
| PySide6 + native deps on weird distros                | pixi pulls Qt from conda-forge → consistent across distros          |
| OAuth client-secret leakage                           | Embed app's client ID only; use PKCE so secret is not required;     |
|                                                       | accept that desktop OAuth clients are inherently public             |

---

## 11. Verification — v0.1 acceptance script

A run through these steps must pass before tagging v0.1.

1. Fresh clone → `pixi install` → `pixi run migrate` → `pixi run run`
   launches the app with an empty event list.
2. **CalDAV.** Start `pixi run radicale`. Add a CalDAV account pointing
   at `http://localhost:5232`. Initial sync completes; events created
   directly in Radicale (via `curl`) appear in Month view.
3. Drag a new event onto Week view → appears in Radicale within 30 s.
   Resize its end edge → server reflects new end time.
4. **Google.** Add a Google account via OAuth wizard
   (loopback to `http://127.0.0.1:<random>`). Existing events appear after
   initial sync. Edit an event title → appears in Google web UI < 30 s.
5. **Microsoft.** Same with an Outlook/Microsoft account.
6. Quit, kill network, restart → cached events still visible; new edits
   queue locally. Restore network → queue drains; nothing duplicated.
7. **Conflict.** Edit the same event in app *and* in Google web UI
   simultaneously. On next sync, conflict dialog surfaces; both
   "use local" and "use remote" paths land correctly on the server.
8. **Notification.** Schedule an event 1 minute from now with a
   `VALARM` → desktop notification fires at the right time.
9. **ICS import.** Double-click a `.ics` file in Nautilus → import preview
   dialog opens; accepting it inserts the event(s) into a chosen calendar.
10. **Flatpak.** `pixi run flatpak-build` produces a bundle; the installed
    Flatpak passes steps 1–9 unmodified.

---

## 12. Operational concerns

### 12.1 Logging

- stderr in development.
- `journald` via `systemd.journal.JournalHandler` when available (we are
  a desktop app, journald is the right place).
- Log level configurable via `$LILICAL_LOG_LEVEL`.
- No telemetry. No analytics. No crash reporter that phones home. (Future:
  opt-in local crash dump folder users can attach to issues.)

### 12.2 XDG paths

- DB:           `$XDG_DATA_HOME/lilical/lilical.db`
- Config:       `$XDG_CONFIG_HOME/lilical/config.toml`
- Secrets:      OS keyring (preferred) or `$XDG_DATA_HOME/lilical/credentials.enc`
- Cache:        `$XDG_CACHE_HOME/lilical/`
- Logs:         journald (no on-disk log file unless `$LILICAL_LOG_FILE` set)

### 12.3 Updates

Flatpak handles updates. AppImage users get a checked-on-launch update
notification (no auto-download); link goes to the release page.

### 12.4 Data export

`File → Export…` writes a full iCalendar archive per account. Provides an
escape hatch and protects against any lock-in fear.

### 12.5 License

GPL-3.0-or-later. Matches Karlender and GNOME Calendar (khal is
ISC/MIT — a permissive comparison point, not a match). Compatible
with PySide6's LGPL.

---

## 13. Reference projects (study, don't vendor blindly)

- **vdirsyncer** (Python, GPL) — canonical Python sync engine for CalDAV;
  read its `Storage` abstraction, conflict UX, and per-server quirks.
- **khal** (Python, MIT) — recurrence handling, iCalendar handling,
  account config patterns.
- **Karlender** (Rust + GTK4, GPL-3) — UI layout for a CalDAV-only Linux
  calendar; the closest existing prior art.
- **GNOME Calendar / KOrganizer** — desktop calendar UX conventions.
- **Maestral** (PySide6 + qasync + Flathub) — cleanest end-to-end example
  of the exact stack we're using; study its Flatpak manifest, qasync
  shutdown handling, tray, notifications.
- **dCalendar**, **PyQt-Fluent-Widgets** — Qt calendar widget patterns.
- **Business Calendar** (Android, closed) — visual reference only.

---

## 14. Open questions (parked, not blocking v0.1)

1. **Theming editor.** Defer to v0.2. v0.1 ships two themes (light, dark).
2. **CalDAV server quirk matrix.** We'll build shims as we encounter
   failures during M1's integration testing.
3. **Multi-window.** v0.1 is single-window. Multi-window agenda comes when
   users ask.
4. **Mobile.** Not v0.1. Qt-for-Python-on-mobile is immature; if a mobile
   port happens it's a future Kirigami rewrite or a sister app.
5. **i18n.** All strings via `QCoreApplication.translate`. Locale files
   live in `data/translations/`. v0.1 ships English; community translations
   from v0.2 onward via Weblate.
