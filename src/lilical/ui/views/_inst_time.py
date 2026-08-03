"""Display-zone datetimes for `EventInstanceRow`s, all-day-safe.

Every view renders instances through these two helpers rather than calling
`.astimezone()` directly, so the whole grid follows the user's chosen display
zone.

**Invariant — occurrence identity.** These values do not only drive pixels.
`_compute_*_placements` puts the result in the placement dict as
`instance_dtstart`; it reaches `EventChip.instance_dtstart`, then
`dispatch_drag_edit` / the per-occurrence details, edit, delete and copy
handlers, and ends up as the `recurrence_id_dt` passed to
`EventStore.queue_update_instance` and `queue_split_series`. There it is reduced
to an integer by `lilical.recurrence.identity.recurrence_key`, which keys timed
slots on the **UTC epoch-minute** and all-day slots on the **wall-clock date**.

So both branches below are chosen to be identity-preserving:

* timed — `to_display` only respells the offset, so the instant is unchanged;
* all-day — `.replace(tzinfo=...)` only respells the zone, so `.date()` is
  unchanged.

Do not "simplify" the all-day branch into a plain conversion. It would change
the instant *and* the date, which silently duplicates override rows and loses
per-occurrence edits and deletions.
"""

from __future__ import annotations

from datetime import datetime

from lilical.utils.timezone import display_zone, to_display


def _convert(raw_iso: str | None, all_day: bool) -> datetime | None:
    try:
        raw = datetime.fromisoformat(raw_iso)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    if all_day:
        # All-day rows are anchored at *system-local* midnight by
        # EventStore._anchor_all_day, so their stored offset carries no
        # meaning — converting them would slide the chip a day. Read the wall
        # clock verbatim and stamp it with the display zone so downstream
        # .date(), .hour, .minute and strftime behave exactly as before.
        return raw.replace(tzinfo=display_zone())
    return to_display(raw)


def inst_start(inst) -> datetime | None:
    """Display-zone start of an instance, or None if unparseable."""
    return _convert(inst.dtstart_local, bool(getattr(inst, "all_day", 0)))


def inst_end(inst) -> datetime | None:
    """Display-zone end of an instance, or None if unparseable."""
    return _convert(inst.dtend_local, bool(getattr(inst, "all_day", 0)))
