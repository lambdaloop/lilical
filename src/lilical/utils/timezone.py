"""Timezone resolution, and the app-wide *display* zone.

Two distinct notions live here, and confusing them causes real bugs:

- `local_iana_tz()` / `local_zoneinfo()` — the **OS** zone. Storage and the
  backend ingest boundaries anchor all-day events against this, so it must stay
  independent of any user preference or the same feed would produce different
  rows depending on what the user happened to be looking at.
- `display_*` — the **display** zone the user picked in the toolbar. Every
  render path reads it at render time, mirroring the `ui.theme` idiom, so
  changing it only requires triggering a repaint/refresh afterwards.
"""

from __future__ import annotations

import functools
import os
import zoneinfo
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Slack added to each end of a DB instance query. All-day rows are anchored at
# *system-local* midnight while the query window is computed from *display*-zone
# midnight, and the IANA offset range (-12..+14) spans 26 h — enough for a 24 h
# all-day interval to fall outside the window entirely. 30 h covers the spread
# plus DST slop. Every view already clips out-of-range instances at render time,
# so over-fetching is free.
QUERY_PAD = timedelta(hours=30)


def local_iana_tz() -> str:
    """Return the system's IANA timezone name.

    `datetime.now().astimezone().tzinfo` may yield a fixed-offset
    `datetime.timezone` without a `.key` attribute rather than a
    `zoneinfo.ZoneInfo`.  Fall through a chain of OS hints before
    defaulting to "UTC".
    """
    tz = datetime.now().astimezone().tzinfo
    name = getattr(tz, "key", None)
    if name:
        return name
    try:
        with open("/etc/timezone") as f:
            candidate = f.read().strip()
        if candidate:
            ZoneInfo(candidate)
            return candidate
    except (OSError, ZoneInfoNotFoundError):
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            candidate = link.split("zoneinfo/", 1)[1]
            ZoneInfo(candidate)
            return candidate
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    return "UTC"


def local_zoneinfo() -> ZoneInfo:
    try:
        return ZoneInfo(local_iana_tz())
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


# ── Display zone ──────────────────────────────────────────────────────────
#
# Held as a single immutable tuple rebound atomically, not as two globals.
# `_query_*_data` runs under `asyncio.to_thread` while `set_display_tz` runs on
# the main thread; one rebind means a query can never observe the name and the
# zone disagreeing, or two accessors within one query resolving differently.

_state: tuple[str, ZoneInfo] = (local_iana_tz(), local_zoneinfo())


def set_display_tz(name: str) -> bool:
    """Set the app-wide display zone.

    Returns False and keeps the previous zone if `name` is not resolvable.
    Mirrors `ui.theme.apply()`: callers only need to trigger a repaint or
    refresh afterwards.
    """
    global _state
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    _state = (name, zone)
    return True


def display_tz_name() -> str:
    """IANA name of the active display zone."""
    return _state[0]


def display_zone() -> ZoneInfo:
    """The active display zone."""
    return _state[1]


def to_display(dt: datetime) -> datetime:
    """Convert `dt` into the display zone.

    A naive `dt` is interpreted as already being display-zone wall clock —
    deliberately unlike a bare `.astimezone()`, which would read it as
    system-local.
    """
    zone = _state[1]
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def display_now() -> datetime:
    """Now, in the display zone."""
    return datetime.now(_state[1])


def display_today() -> date:
    """Today's date in the display zone."""
    return datetime.now(_state[1]).date()


def display_midnight(d: date) -> datetime:
    """Midnight of `d` in the display zone, as an aware datetime."""
    return datetime(d.year, d.month, d.day, tzinfo=_state[1])


def zone_exists(name: str) -> bool:
    """True if `name` resolves to a known IANA zone."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


@functools.cache
def iana_zones() -> tuple[str, ...]:
    """Sorted IANA zone names.

    Cached and lazy on purpose: `available_timezones()` walks the tzdata tree
    (~10-20 ms), and this module is imported at startup by
    `storage.event_store`, so a module-level constant would be pure cold-start
    cost for something only the pickers need.
    """
    return tuple(sorted(zoneinfo.available_timezones()))
