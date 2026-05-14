# lilical — UI Specification

Companion to [PLAN.md](PLAN.md). The visual reference is **Business
Calendar 2** for Android; this document is a desktop translation of its
density, layout, and interaction grammar.

The five views — Month, Week (1–14 days), Day, Year, Agenda — each map
1:1 onto a Business Calendar 2 view, with adaptations for mouse +
keyboard.

---

## 1. Visual language

### 1.1 Palette (default dark theme)

| Token                | Hex          | Used for                                       |
| -------------------- | ------------ | ---------------------------------------------- |
| `--bg-base`          | `#1c1c1c`    | window background                              |
| `--bg-surface`       | `#262626`    | day cells, panels                              |
| `--bg-surface-alt`   | `#2e2e2e`    | sidebar, today highlight                       |
| `--bg-weekend`       | `#222222`    | weekend column tint (BC2 custom-weekday-bg)    |
| `--border`           | `#3a3a3a`    | grid lines                                     |
| `--border-strong`    | `#555555`    | view divider, today border                     |
| `--text-primary`     | `#e8e8e8`    | event titles, day numbers                      |
| `--text-secondary`   | `#a8a8a8`    | spillover days, time-axis labels               |
| `--text-disabled`    | `#666666`    | declined events                                |
| `--accent`           | `#5e9fff`    | today's day-number ring, primary buttons       |
| `--accent-hover`     | `#7eb5ff`    |                                                |
| `--danger`           | `#e25c5c`    | conflict pill, delete confirm                  |
| `--success`          | `#5cc97a`    | sync OK pill                                   |

### 1.2 Light theme

Same token names, different values:

| Token                | Hex          |
| -------------------- | ------------ |
| `--bg-base`          | `#fafafa`    |
| `--bg-surface`       | `#ffffff`    |
| `--bg-surface-alt`   | `#f5f5f5`    |
| `--bg-weekend`       | `#f3f3f3`    |
| `--border`           | `#d8d8d8`    |
| `--border-strong`    | `#9a9a9a`    |
| `--text-primary`     | `#1a1a1a`    |
| `--text-secondary`   | `#5a5a5a`    |
| `--text-disabled`    | `#9a9a9a`    |
| `--accent`           | `#2563eb`    |
| `--accent-hover`     | `#1d4ed8`    |
| `--danger`           | `#c44545`    |
| `--success`          | `#2f8c4a`    |

Accent/danger/success are shifted darker than the dark-theme values
to keep the **same** WCAG contrast against the lighter surfaces.
The user's accent-color override replaces `--accent` in both themes.
We do **not** ship 22 themes like BC2 in v0.1; just light + dark
with the accent override.

### 1.3 Type

- Body: `Inter`, fallback `system-ui`, 10 pt at default zoom.
- Day numbers: 14 pt, weight 500.
- Time axis: 9 pt, weight 400, secondary color.
- Event chip title: 10 pt, weight 500, primary color.
- Event chip time prefix: 9 pt, weight 400, 80% opacity.

Font size is a setting (BC2 ships this); ranges 8–14 pt. All sizes are
relative to a single `--font-base` token.

### 1.4 Spacing & radii

- Day-cell padding 4 px.
- Event chip vertical padding 2 px, horizontal 6 px.
- Chip corner radius 4 px.
- Sidebar width 240 px (resizable, persisted).
- Toolbar height 44 px.
- Status bar height 22 px.

### 1.5 Event chip rendering

Three modes (BC2 parity), set per view:

1. **Bars** — Solid filled rectangle in the event's color. Title overlaid
   in white (or dark if color is light). Used in Month text-mode-off and
   in Week/Day views. Time prefix optional.
2. **Text** — Colored left-border (3 px), neutral background, title in
   primary text color. Compact. Used in Month text-mode-on.
3. **Dot** — A 6 px colored disk + title beside it. Used when a row only
   has space for one line.

Color comes from the event itself, else from the calendar's color.
For decoded color contrast, we compute readable text color via WCAG
contrast at chip-build time.

---

## 2. Window chrome

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ‹ today › ┊ Month  Week  Day  Year  Agenda  │  May 2026  │ ✏ + ⟳  ⚙   │  toolbar
├──────────┬──────────────────────────────────────────────────────────────┤
│          │                                                              │
│ MINI ◅ ▻ │                                                              │
│ ┌──────┐ │                                                              │
│ │ May  │ │                                                              │
│ │      │ │                                                              │
│ └──────┘ │                                                              │
│          │                                                              │
│ FAV BAR  │                       MAIN VIEW                              │
│ ◉ Work   │                                                              │
│ ◉ Home   │                                                              │
│          │                                                              │
│ CALS     │                                                              │
│ ☑ Work   │                                                              │
│ ☑ Home   │                                                              │
│ ☐ Travel │                                                              │
│ + Add    │                                                              │
│          │                                                              │
├──────────┴──────────────────────────────────────────────────────────────┤
│ ● synced 2 m ago  │  Work: 142 events  │  ⚠ Couldn't reach Outlook ▾  │  status
└─────────────────────────────────────────────────────────────────────────┘
```

- **Toolbar (top):** ‹ today › buttons (prev / today / next), view
  switcher tab strip, current range label, quick-add (✏), refresh-now
  (⟳), preferences (⚙). The view switcher highlights the active view.
- **Sidebar (left):**
  - Mini-month picker (header with month name + arrows).
  - **Favorite Bar** (BC2 parity): one-click jump to a saved set of
    calendars visible / hidden.
  - Calendar list with visibility checkboxes and color swatches; grouped
    by account; "+ Add account" at bottom.
- **Status bar (bottom):** sync state pill (green/orange/red), per-
  account counts on hover, error pill that expands to a details popover
  on click.

---

## 3. Month view

The default landing view. Six-row × seven-column grid. Spillover days
from the previous/next month are rendered in `--text-secondary`. Today
is marked with a circle around the day number in `--accent`.

```
┌─────────┬─────────┬─────────┬─────────┬─────────┬─────────┬─────────┐
│  MON    │  TUE    │  WED    │  THU    │  FRI    │  SAT    │  SUN    │
│   28    │   29    │   30    │    1    │    2    │    3    │    4    │
│         │         │         │         │         │         │         │
│ ▮Stand- │         │ ▮Confer-│ ▮Confer-│ ▮Plan   │         │         │
│  up 9a  │         │  ence   │  ence   │         │         │         │
│         │         │         │         │         │         │         │
│ ▮Lunch  │ ▮Demo   │         │ ▮1:1    │         │         │         │
│         │         │         │         │         │         │         │
├─────────┼─────────┼─────────┼─────────┼─────────┼─────────┼─────────┤
│    5    │    6    │    7    │    8    │    9    │   10    │   11    │
│         │         │         │         │         │         │         │
│ ▮Stand- │ ▮Stand- │ ▮Travel─────────────────────────→     │         │  multi-day bar
│  up     │  up     │                                       │         │
│         │         │ ▮3 more │ ▮Stand- │         │         │         │  overflow
│         │         │         │  up     │         │         │         │
├─────────┼─────────┼─────────┼─────────┼─────────┼─────────┼─────────┤
│   ⦿ 12  │   13    │   14    │ ←today  │   16    │   17    │   18    │
…
```

### 3.1 Modes

Toolbar toggle (per BC2): **Text** vs **Bars**.

- **Bars mode:** solid colored rectangles, title overlaid.
- **Text mode:** colored left-border, title in primary text color.

### 3.2 Overflow

If more events fit than the cell has rows, the last visible row
collapses to `▮N more` (clickable to open the day popover).

### 3.3 Multi-day events

Span across cells as a single bar. Truncated at week boundaries with a
trailing `→` or leading `←`.

### 3.4 Day popover (BC2 parity)

Click any day → popover anchored to the cell, showing every event for
that day as a chronological list with time + title + color chip. Hover
on an event opens an event tooltip; click switches to Day view of that
date.

### 3.5 Interactions

| Gesture          | Action                                            |
| ---------------- | ------------------------------------------------- |
| Click day        | Open day popover                                  |
| Double-click day | Switch to Day view                                |
| Click event      | Event detail popover                              |
| Double-click event | Open edit dialog                                |
| Right-click event | Context menu (Edit / Duplicate / Delete / Move) |
| Drag event       | Move to another day; ghost preview at cursor      |
| Drag empty area  | Create event spanning the drag range; opens new-event dialog on release |
| Ctrl+scroll      | Zoom view (changes cell height)                   |
| Scroll wheel     | Move forward/back by week                         |
| Shift+scroll     | Move forward/back by month                        |

### 3.6 Today

Day number rendered as a filled `--accent` circle; the cell border is
1.5 px `--border-strong` on all sides.

### 3.7 Weekends

`--bg-weekend` tint on Sat/Sun columns. Optional (preference).

---

## 4. Week view

The flagship view; this is where most editing happens. Configurable
day span 1–14 (BC2 parity), via a **day-count slider** in the toolbar.

```
        ┌──── MON 13 ───┬──── TUE 14 ───┬──── WED 15 ───┬──── THU 16 ───┬──── FRI 17 ───┐
all-day │ ▮Conference──────────────────────────────────→                                │
        ├───────────────┼───────────────┼───────────────┼───────────────┼───────────────┤
  07:00 │               │               │               │               │               │
  08:00 │               │               │ ▮Demo prep    │               │               │
  09:00 │ ▮Stand-up     │ ▮Stand-up     │ ▮Stand-up     │ ▮Stand-up     │ ▮Stand-up     │
        │               │ ▮Demo         │               │               │               │
  10:00 │               │               │               │ ▮1:1 Anna     │ ▮Plan         │
        │═══════ now ═══════════════════════════════════════════════════════════════════│
  11:00 │ ▮Lunch prep   │               │ ╔ Lunch ╗     │               │               │
  12:00 │ ▮Lunch        │ ▮Lunch        │ ║        ║    │ ▮Lunch        │ ▮Lunch        │
  13:00 │               │               │ ╚════════╝    │               │               │
  14:00 │               │ ▮Design       │               │ ▮Design       │               │
        │               │ ▮review───────┼ ─review (cont)│ ▮review       │               │
  15:00 │               │               │               │               │               │
        …
```

### 4.1 Layout

- Left **time axis** column, 60 px wide, hours labeled.
- **All-day band** at top (auto-expanding to fit; max 4 rows then scrolls).
- Day columns of equal width.
- One hour = 48 px at default zoom; pinch-equivalent (Ctrl+scroll) ranges
  20–96 px.
- Working-hours default 7am–8pm (BC2 default); scroll exposes the rest.
- Today's column has `--bg-surface-alt` tint.
- The current-time indicator is a thin red line spanning the today column.
- Overlapping events lay out side-by-side with equal column splits; if
  three events overlap, each takes one third of the column width.

### 4.2 Event chips

Bars mode by default. Show time prefix (`09:00`) + title. If chip is
shorter than 24 px, only the title; if shorter than 14 px, the chip is
solid color with no text (hover shows tooltip).

### 4.3 Drag verbs

BC2-style direct manipulation for mouse. Three independent flows:

#### Drag-to-create (empty grid)

| Surface              | Drag direction        | Result                                           |
| -------------------- | --------------------- | ------------------------------------------------ |
| Timed body           | Vertical              | Ghost spans drag extent; release → EventDialog pre-filled with snapped times |
| All-day band (Week)  | Horizontal            | Ghost spans columns; release → EventDialog with all-day checked, correct date range |
| Click (no drag)      | —                     | EventDialog opens with clicked time + 1 h default |

The ghost preview (`DragPreview`) shows a semi-transparent accent rectangle
with a centered label: `HH:MM – HH:MM  (Nh Mm)` for timed, or
`All day · N day(s)` for all-day. Times snap as the cursor moves.

#### Drag-to-move (chip body)

Press in the body zone of a timed chip, move > 4 px: the ghost preview
tracks the cursor. Vertical delta changes start/end times; horizontal delta
(Week only) changes the day column. Release → `queue_update` is called,
no dialog opened. Day view ignores horizontal delta (single-column).

All-day chips in the all-day band support body drag; vertical drag is
ignored (clamped to the all-day band).

#### Drag-to-resize (chip edges)

| Edge zone      | Activated when                     | Action              |
| -------------- | ---------------------------------- | ------------------- |
| Top 6 px       | Chip height ≥ 18 px, timed chip    | Adjusts `dtstart`   |
| Bottom 6 px    | Chip height ≥ 18 px, timed chip    | Adjusts `dtend`     |

Cursor changes to `SizeVerCursor` on hover to reveal the affordance.
Chips shorter than 18 px only support body-drag (resize via dialog).
Resize snaps to the configured snap interval. Minimum duration = snap interval.
End time is clamped at midnight; multi-day timed events are not created.

#### Ghost preview

`DragPreview` is a `QGraphicsItem` at Z=200 (above chips Z=0 and sticky
header Z=100). It is reused across create/move/resize in the same view
and torn down on commit or cancel.

Label formats:
- Create / resize: `HH:MM – HH:MM  (Nh Mm)`
- Move: `DDD  HH:MM – HH:MM`

#### Snap and cancel

- **Snap** applies to all three flows. Configurable 5/10/15/30/60 min
  (default 15). Set in **Preferences → Drag snap interval**.
- **Esc** during any active drag cancels it, removes the ghost, and does
  not commit any change.

### 4.4 Day-count slider

Located in the toolbar when Week is active. Snap stops: 1, 2, 3, 4, 5,
7 (default), 10, 14. The number-of-days choice persists per session.

### 4.5 Zoom

Ctrl+scroll vertical: changes pixel-per-hour. Ctrl+0 resets. The
selected event stays in view across zooms.

### 4.6 Now indicator

A 2 px horizontal `--danger` line spanning the today column with a small
dot at the time-axis end. Updates every 60 s.

---

## 5. Day view

Effectively Week view with `day_count=1`, but rendered with a bit more
horizontal breathing room and a wider all-day band.

```
                ┌─────────────────────────────────────────────────────┐
                │            Thursday, May 13, 2026                   │
                ├─────────────────────────────────────────────────────┤
        all-day │ ▮Conference (Day 2 of 3)                            │
                ├─────────────────────────────────────────────────────┤
          07:00 │                                                     │
          08:00 │ ▮Demo prep                                          │
          09:00 │ ▮Stand-up         ▮Sync w/ Pat                      │
          10:00 │ ▮1:1 Anna                                           │
                │══════════════ now ══════════════════════════════════│
          11:00 │ ▮Lunch prep                                         │
          12:00 │ ▮Lunch                                              │
          …
```

Day-view-specific: a small **mini-agenda** at the bottom shows the next
3 upcoming events from any visible calendar (BC2 includes a similar
"today's events" strip).

### 5.1 Drag verbs in Day view

Same three flows as §4.3 (drag-to-create, drag-to-move, drag-to-resize)
with two simplifications:

- **Horizontal delta is ignored** during chip-move. The Day view has a
  single column; moving an event to a different day requires the dialog
  or switching to Week view.
- **All-day create** drag always produces a single-day event (there is no
  horizontal multi-day extent to drag in a one-column layout).

---

## 6. Year view

Heat-map style overview. Twelve mini-months in a 3×4 or 4×3 grid;
each day cell is tinted by event density.

```
              ─────────────────────── 2026 ───────────────────────
              ┌────────────────┬────────────────┬────────────────┐
              │     January    │   February     │     March      │
              │  M T W T F S S │  M T W T F S S │  M T W T F S S │
              │      1 2 3 4 5 │           1 2  │              1 │
              │  6 7 ▒ 9 ▒ ▒ 12│  3 ▒ 5 ▒ 7 ▒ 9 │  2 ▒ ▒ 5 ▒ 7 8 │
              │ 13 ▒ ▒ ▒ ▒ ▒ 19│ 10 ▒ ▒ ▒ ▒ ▒ 16│  9 ▒ ▒ ▒ ▒ ▒ 15│
              …
              └────────────────┴────────────────┴────────────────┘
```

Tint scale (3 levels above the neutral "no events" cell): 0 / 1–3 /
4–6 / 7+ events. The tint hue is always `--accent` (alpha-blended on
the cell background); we **don't** try to mix per-calendar colors
into a heat-map cell — it would muddy badly with 3+ calendars and
break colorblind users. The legend below the year grid shows the
density-to-shade mapping. Hover
shows count tooltip. Click → switch to Month view of that month. Day
within a mini-month → switch to Day view of that date.

---

## 7. Agenda view

Virtualized chronological list. BC2's Agenda includes multi-select for
batch operations; ours does too.

```
┌───────────────────────────────────────────────────────────────────────┐
│  ☐  Mon, May 13                                                       │
│  ☐  09:00 – 09:30  ▮ Stand-up                              Work       │
│  ☐  10:00 – 11:00  ▮ 1:1 with Anna                         Work       │
│  ☐  12:00 – 13:00  ▮ Lunch                                 Personal   │
│  ☐  14:00 – 16:00  ▮ Design review                         Work       │
│                                                                       │
│  ☐  Tue, May 14                                                       │
│  ☑  09:00 – 09:30  ▮ Stand-up                              Work       │
│  ☑  09:30 – 11:00  ▮ Demo                                  Work       │
│  ☐  14:00 – 15:30  ▮ Design review (cont)                  Work       │
│  …                                                                    │
└───────────────────────────────────────────────────────────────────────┘
   Selected: 2    [ Delete ]  [ Move… ]  [ Copy… ]  [ Change calendar… ]
```

### 7.1 Interactions

- **Click row** = open event popover.
- **Double-click row** = edit dialog.
- **Click checkbox / Space when focused** = toggle selection.
- **Shift+Click** = range select.
- **Ctrl+A** = select all visible.
- **Delete** when selection non-empty = delete with confirm.
- **Bottom action bar** appears when selection is non-empty.

### 7.2 Grouping

Default: by day. Optional: by week. The day header is sticky to the
top of the viewport while scrolling.

### 7.3 Virtualization

`QAbstractItemView` with a custom delegate. Pre-render ±200 items.

---

## 8. Event dialog

Used for both create and edit.

```
┌─ New event ────────────────────────────────────────────────────┐
│                                                                │
│  Title:           [ Design review                            ] │
│                                                                │
│  Starts:          [ 2026-05-13 ] [ 14:00 ]   ☐ All day         │
│  Ends:            [ 2026-05-13 ] [ 16:00 ]                     │
│  Time zone:       [ Europe/Berlin                            ▾]│
│                                                                │
│  Calendar:        [ ● Work (Google)                          ▾]│
│  Color:           [ ●●●●●●● ●●● + ] (use calendar's color)     │
│                                                                │
│  Location:        [ Room 3                                   ] │
│  URL:             [                                          ] │
│                                                                │
│  Repeats:         [ Does not repeat                          ▾]│
│  Reminder:        [ 10 min before                            ▾] [+]│
│                                                                │
│  Attendees:       [ anna@example.com ✕  bob@example.com ✕  + ] │
│                                                                │
│  Notes:                                                        │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                                                           │ │
│  │                                                           │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                │
│  Status:          ● Confirmed  ○ Tentative  ○ Cancelled        │
│  Visibility:      ● Busy  ○ Free                               │
│                                                                │
│              [ Cancel ]            [ Save ]                    │
└────────────────────────────────────────────────────────────────┘
```

### 8.1 Repeats

`Does not repeat | Daily | Weekly on <day> | Monthly | Yearly | Custom…`
Selecting `Custom…` opens a sub-dialog that builds an RRULE.

### 8.2 Reminders

Each adds a row: `[N] [minutes|hours|days] before [+]`. Multiple
reminders supported. Stored as `VALARM` blocks.

### 8.3 Recurring edits

When editing an event that repeats, on Save we ask:

```
┌─ Edit recurring event ────────────────────────┐
│  Apply changes to:                            │
│  ○ Only this occurrence                       │
│  ● This and future occurrences                │
│  ○ All occurrences                            │
│           [ Cancel ]   [ Save ]               │
└───────────────────────────────────────────────┘
```

Mirrors Google Calendar / Apple Calendar; familiar to users.

---

## 9. Account setup wizard

Three pages: pick provider → authorize → choose calendars.

```
┌─ Add an account ──────────────────────────────────────────────┐
│  Step 1 of 3 — What kind of account?                          │
│                                                               │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│   │     [G]      │  │    [▢▢▢]     │  │    [📅]      │        │
│   │   Google     │  │  Microsoft   │  │   CalDAV     │        │
│   │   Calendar   │  │   / Outlook  │  │              │        │
│   └──────────────┘  └──────────────┘  └──────────────┘        │
│                                                               │
│                                       [ Cancel ]  [ Next > ]  │
└───────────────────────────────────────────────────────────────┘
```

- **Google / Microsoft**: opens browser to OAuth consent screen
  (loopback redirect to `127.0.0.1:<random>`). After consent, we show
  "Connected as <email>" and proceed.
- **CalDAV**: form with `Server URL`, `Username`, `Password / App
  password`, optional `Display name`. We try `.well-known/caldav`
  discovery, then probe principal URL.

Step 3 lists discovered calendars with checkboxes; default all on.

---

## 10. Conflict dialog

```
┌─ Sync conflict ──────────────────────────────────────────────────┐
│  "Design review" was changed both here and on the server.        │
│                                                                  │
│           Your version              │       Server version       │
│  ─────────────────────────────────────────────────────────────── │
│  Title:    Design review (final)    │       Design review        │
│  Start:    2026-05-13  14:00        │       2026-05-13  14:00    │
│  End:      2026-05-13  17:00        │  ⚠    2026-05-13  16:00    │
│  Where:    Room 3                   │  ⚠    Room 4               │
│                                                                  │
│  ○ Keep your version (overwrite server)                          │
│  ○ Use server version (discard your changes)                     │
│  ● Merge: I'll edit before saving                                │
│                                                                  │
│                            [ Cancel ]      [ Continue ]          │
└──────────────────────────────────────────────────────────────────┘
```

Conflicting fields are marked with `⚠`. Choosing **Merge** opens the
event dialog pre-populated with a synthesized version (UI lets you
pick field-by-field via tiny chevrons).

---

## 11. Quick-add (tray + Ctrl+Shift+A)

Single text-input popover. Parses natural language ("Lunch with Anna
tomorrow at 1pm at the cafe"); previews the parsed event below the
input; Enter saves to the default calendar.

```
┌─ Quick add ────────────────────────────────┐
│  > Lunch with Anna tomorrow at 1pm         │
│                                            │
│   Tomorrow Thu May 14, 13:00 – 14:00       │
│   "Lunch with Anna"                        │
│   Calendar: ● Work (Google)            ▾   │
│                                            │
│                          [ Save ]          │
└────────────────────────────────────────────┘
```

Natural-language parsing uses **`dateparser`** for the date/time
substring (handles "tomorrow at 1pm", "next Friday", locale-aware
formats, relative dates) plus a small regex layer to extract the
title, location keyword (`at <place>`, `in <place>`) and calendar
hint (`@Work`). No LLM. The parse is always shown for confirmation —
we accept that it'll be wrong sometimes.

---

## 12. Keyboard shortcuts

| Shortcut         | Action                                             |
| ---------------- | -------------------------------------------------- |
| `1` / `2` / `3` / `4` / `5` | Switch to Month / Week / Day / Year / Agenda |
| `t`              | Jump to today                                      |
| `←` / `→`        | Previous / next period (month/week/day)            |
| `Page Up/Down`   | Previous / next month (in Month view), week (Week) |
| `n` or `Ctrl+N`  | New event                                          |
| `Ctrl+Shift+A`   | Quick add (Ctrl+Q is reserved for Quit per Linux convention) |
| `e`              | Edit selected event                                |
| `Delete`         | Delete selected event(s) (with confirm)            |
| `Ctrl+D`         | Duplicate selected event                           |
| `j` / `k`        | Next / previous event in current view              |
| `h` / `l`        | Previous / next day (Day/Week)                     |
| `Ctrl+R`         | Force refresh now                                  |
| `Ctrl+F`         | Search (v0.2; placeholder shortcut)                |
| `Ctrl+,`         | Preferences                                        |
| `Ctrl++` / `Ctrl+-` | Zoom in / out (Week/Day)                        |
| `Ctrl+0`         | Reset zoom                                         |
| `Esc`            | Cancel current drag / close popover / dialog       |
| `F11`            | Toggle full-screen                                 |
| `?`              | Show shortcut overlay                              |

The shortcut overlay is a single modal showing all bindings; closes on
any key.

---

## 13. Accessibility

- Every actionable item: `setAccessibleName` + `setAccessibleDescription`.
- Tab traversal order curated; no implicit focus loops.
- Color is **never** the only signal: today's day has a circle around
  the number, not just a colored cell; conflicts use a `⚠` glyph plus
  color; selected items have a 2 px border, not just a tint.
- Contrast ≥ 4.5:1 for body text, ≥ 3:1 for large text (WCAG AA).
- High-contrast mode auto-detected from `QStyleHints.colorScheme()`.
- Screen reader: Orca-tested. Calendar grid exposes its semantic
  structure via `QAccessible.Role.Table`. Each event chip exposes:
  - Name: event title
  - Description: "<start time> to <end time> on <date>, calendar <name>"
  - Role: `Button`
- Qt has no first-class "reduce motion" hint, so we ship a Preferences
  toggle **Reduce motion** that disables drag-ghost animations and
  tween transitions. Default is auto-detected from the desktop
  environment where possible (GNOME `enable-animations`, KDE
  `kdeglobals/KDE/AnimationDurationFactor`); otherwise on.

---

## 14. Theming tokens — where they live

All theme tokens are defined as Qt properties on `QApplication.palette()`
plus a small `--var` substitution layer in our QSS preprocessor (we
ship a tiny string-replace step; no full preprocessor).

```
src/lilical/ui/styles/
├── _tokens-dark.qss     # @define-color-like declarations
├── _tokens-light.qss
└── lilical.qss          # uses the tokens
```

User accent-color override writes a `--accent` value to the active
tokens file at startup.

---

## 15. Preferences dialog (`Ctrl+,`)

Opened from the **⚙** toolbar button or `Ctrl+,`. Persisted to
`QSettings` under `~/.config/lilical/lilical.conf`.

| Setting                 | Options                                   | Default   | Notes |
| ----------------------- | ----------------------------------------- | --------- | ----- |
| Theme                   | dark / light                              | dark      |       |
| Week starts on          | Monday / Sunday / Saturday                | Monday    |       |
| Default view            | Month / Week / Day / Agenda               | Week      | View shown at launch |
| Drag snap interval      | 5 / 10 / 15 / 30 / 60 min                 | 15 min    | Applied to create, move, and resize drags in Week and Day views |

The snap interval is applied in real-time (no restart needed) and
persisted across sessions. Changing it while a drag is in progress takes
effect on the next drag.

---

## 17. Behaviors NOT borrowed from Business Calendar 2

We intentionally drop:

| BC2 feature                        | Why we skip it (v0.1)                      |
| ---------------------------------- | ------------------------------------------ |
| Tasks view                         | Deferred to v0.2                           |
| Weather integration                | Pulls in another API + privacy concern     |
| Widgets (Android home-screen)      | n/a on Linux desktop                       |
| Emoticons in events                | Just use any Unicode emoji; no picker      |
| 22 themes                          | Light + dark + accent override is enough   |
| Stickers / per-account icon packs  | Out of scope                               |
| Custom weekday backgrounds         | Weekend tint only                          |

---

## 18. Open visual questions (parked)

1. **Multi-day event packing.** When 5+ multi-day events overlap in
   a Month-view week row, do we stack inside the row (taller cell) or
   collapse to a "+3 multi-day" chip? Defer until M4 reveals real
   behavior.
2. **Localized week start.** Mon/Sun/Sat defaults from locale; user can
   override. Locked.
3. **Day-of-week column width parity.** Some calendars give weekends a
   narrower column. We won't in v0.1 (consistent grid is simpler).
