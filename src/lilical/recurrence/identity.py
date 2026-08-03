"""Stable integer identity for a recurrence slot.

A RECURRENCE-ID names one slot in a series, but the same slot has many valid
ISO-8601 spellings: ``2026-05-20T09:00:00+00:00``, ``...T09:00:00Z``,
``...T11:00:00+02:00``, or the same wall clock under a different DST offset.
Each backend supplies its own spelling on read-back, and the UI supplies a
*fixed-offset* one — ``datetime.fromisoformat(inst.dtstart_local).astimezone()``
can never yield a named zone. Comparing those as strings silently fails, which
duplicates override rows and loses per-occurrence edits and deletions.

So identity is an integer, derived from the instant, and the provider's own
``recurrence_id`` string is preserved untouched for outbound requests.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

__all__ = ["MASTER_KEY", "recurrence_key", "recurrence_id_matches"]

# Master rows carry recurrence_id "" — they are not a slot in any series.
MASTER_KEY = 0


def recurrence_key(dt: datetime | date | None, *, all_day: bool = False) -> int:
    """Integer identity for a recurrence slot. ``MASTER_KEY`` when there is none.

    Timed slots key on the UTC epoch-minute, which absorbs sub-minute jitter and
    every offset spelling of the same instant. All-day slots key on UTC midnight
    of the wall-clock date, which collapses the local-midnight, UTC-midnight and
    bare-``DATE`` spellings that providers disagree about onto one value.
    """
    if dt is None:
        return MASTER_KEY
    if all_day:
        d = dt.date() if isinstance(dt, datetime) else dt
        anchored = datetime.combine(d, time.min, tzinfo=timezone.utc)
        return round(anchored.timestamp() / 60.0)
    if not isinstance(dt, datetime):
        # A bare date used as a timed recurrence-id: treat midnight UTC as the
        # instant rather than guessing a zone.
        dt = datetime.combine(dt, time.min, tzinfo=timezone.utc)
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return round(aware.timestamp() / 60.0)


def recurrence_id_matches(
    a: datetime | date | None,
    b: datetime | date | None,
    *,
    all_day: bool = False,
) -> bool:
    """True when both name the same recurrence slot, whatever their spelling."""
    return recurrence_key(a, all_day=all_day) == recurrence_key(b, all_day=all_day)


def parse_recurrence_key(recurrence_id: str | None, *, all_day: bool = False) -> int:
    """``recurrence_key`` for a stored ISO string. ``MASTER_KEY`` when empty."""
    if not recurrence_id:
        return MASTER_KEY
    try:
        return recurrence_key(datetime.fromisoformat(recurrence_id), all_day=all_day)
    except ValueError:
        return MASTER_KEY
