"""Live end-to-end check: does deleting one occurrence of a series stay deleted?

The unit suite reproduces the bug against fakes; this exercises the real
provider. It creates its own recurring event, deletes the third occurrence,
runs two full sync ticks, and asserts the occurrence is still gone afterwards.
The second tick is the one that used to fail: the local tombstone expired the
moment its pending op uploaded, and the master upsert that followed resurrected
the occurrence.

Usage:
    pixi run python tools/roundtrip_recurring.py --list
    pixi run python tools/roundtrip_recurring.py --account <id-or-name> --yes
    pixi run python tools/roundtrip_recurring.py --account <id> --yes --all-day
    pixi run python tools/roundtrip_recurring.py --account <id> --yes --keep

Safety: this only ever writes events whose summary carries the LILICAL-RT-
marker and whose uid it created in this run. It refuses to touch anything else,
and deletes what it created before exiting (unless --keep).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from the project root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy.orm import Session  # noqa: E402

from lilical.backends.factory import build_backend_factory  # noqa: E402
from lilical.config import _default_db_path  # noqa: E402
from lilical.models.account import Account  # noqa: E402
from lilical.models.calendar import Calendar  # noqa: E402
from lilical.models.event import Event, EventInstanceRow  # noqa: E402
from lilical.storage.db import open_engine  # noqa: E402
from lilical.storage.event_store import EventStore  # noqa: E402
from lilical.storage.secrets import SecretsStore  # noqa: E402
from lilical.sync.engine import SyncEngine  # noqa: E402
from lilical.utils.timezone import local_iana_tz, local_zoneinfo  # noqa: E402

MARKER = "LILICAL-RT-"

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


class Guard:
    """Refuses every write that is not to an event this run created."""

    def __init__(self) -> None:
        self._owned: set[str] = set()

    def own(self, uid: str) -> None:
        self._owned.add(uid)

    def check(self, event: Event) -> None:
        if event.uid not in self._owned:
            raise SystemExit(f"REFUSING to touch a uid we did not create: {event.uid}")
        if MARKER not in (event.summary or ""):
            raise SystemExit(f"REFUSING to touch an unmarked event: {event.summary!r}")


def _instances(engine, uid: str) -> list[EventInstanceRow]:
    with Session(engine) as s:
        return (
            s.query(EventInstanceRow)
            .filter_by(uid=uid)
            .order_by(EventInstanceRow.dtstart_utc)
            .all()
        )


def _report(engine, uid: str, label: str) -> list[int]:
    rows = _instances(engine, uid)
    starts = [r.dtstart_utc for r in rows]
    print(f"  {label}: {len(rows)} occurrences")
    for r in rows:
        print(f"      {r.dtstart_local}  override={bool(r.is_override)}")
    return starts


async def _tick(sync: SyncEngine, account, backend, n: int) -> None:
    print(f"\n── sync tick {n} ─────────────────────────────────────────────")
    await sync._tick(account, backend)


async def run(account_name: str, *, all_day: bool, keep: bool) -> int:
    db_path = _default_db_path()
    print(f"DB: {db_path}")
    engine = open_engine(db_path)

    with Session(engine) as s:
        accounts = s.query(Account).all()
        match = [
            a
            for a in accounts
            if account_name in (a.id, a.display_name, a.identity)
        ]
        if not match:
            print(f"No account matching {account_name!r}. Known accounts:")
            for a in accounts:
                print(f"  {a.id}  {a.kind:12} {a.display_name}  {a.identity}")
            return 2
        account = match[0]
        cals = s.query(Calendar).filter_by(account_id=account.id).all()
        # Prefer a dedicated test calendar; otherwise the primary/first one.
        cal = next(
            (c for c in cals if c.display_name.lower() == "lilical-test"),
            next((c for c in cals if c.is_primary), cals[0] if cals else None),
        )
        if cal is None:
            print("Account has no calendars.")
            return 2
        account_kind, account_label = account.kind, account.display_name
        cal_id, cal_name = cal.id, cal.display_name

    print(f"Account: {account_label} ({account_kind})")
    print(f"Calendar: {cal_name}\n")

    store = EventStore(engine)
    secrets = SecretsStore()
    factory = build_backend_factory(secrets)
    backend = factory(account)
    sync = SyncEngine(store, secrets=secrets, factory=factory)
    guard = Guard()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary = f"{MARKER}{stamp}"
    uid = str(uuid.uuid4())
    guard.own(uid)

    tz = local_zoneinfo()
    base = (datetime.now(tz) + timedelta(days=3)).replace(
        hour=0 if all_day else 11, minute=0, second=0, microsecond=0
    )
    event = Event(
        uid=uid,
        calendar_id=cal_id,
        summary=summary,
        description="Temporary event created by tools/roundtrip_recurring.py",
        dtstart=base,
        dtend=base + (timedelta(days=1) if all_day else timedelta(hours=1)),
        tz=local_iana_tz(),
        all_day=all_day,
        rrule="FREQ=DAILY;COUNT=5",
    )
    guard.check(event)

    failures = 0
    try:
        print(f"1. Creating {summary} ({'all-day' if all_day else 'timed'})")
        store.queue_create(event)
        await _tick(sync, account, backend, 1)

        # mark_synced may rewrite the uid to the provider's canonical one.
        with Session(engine) as s:
            row = next(
                (
                    r
                    for r in s.query(EventInstanceRow)
                    .filter_by(calendar_id=cal_id)
                    .all()
                    if (ev := store.get_event(r.uid, cal_id))
                    and ev.summary == summary
                ),
                None,
            )
        if row is None:
            print(f"{_FAIL} test event never materialized; aborting")
            return 1
        uid = row.uid
        guard.own(uid)

        print("\n2. Local state after create")
        before = _report(engine, uid, "after create")
        if len(before) < 3:
            print(f"{_FAIL} expected 5 occurrences, got {len(before)}")
            return 1

        target = _instances(engine, uid)[2]
        target_utc = target.dtstart_utc
        rid = datetime.fromisoformat(target.dtstart_local).astimezone()
        print(f"\n3. Deleting occurrence 3 of 5: {target.dtstart_local}")
        store.queue_delete_instance(uid, cal_id, rid)

        after_local = _report(engine, uid, "after local delete")
        if target_utc in after_local:
            print(f"{_FAIL} occurrence did not disappear locally")
            failures += 1
        else:
            print(f"  {_PASS} gone locally")

        await _tick(sync, account, backend, 2)
        after_push = _report(engine, uid, "after push+pull")
        if target_utc in after_push:
            print(f"  {_FAIL} occurrence came back after the first sync")
            failures += 1
        else:
            print(f"  {_PASS} still gone after push+pull")

        await _tick(sync, account, backend, 3)
        after_second = _report(engine, uid, "after second tick")
        if target_utc in after_second:
            # This is the regression the whole change is about.
            print(f"  {_FAIL} occurrence resurrected on the second sync")
            failures += 1
        else:
            print(f"  {_PASS} still gone after a second sync")

        if len(after_second) != len(before) - 1:
            print(
                f"  {_FAIL} expected {len(before) - 1} occurrences, "
                f"got {len(after_second)} — the rest of the series was affected"
            )
            failures += 1
        else:
            print(f"  {_PASS} the other {len(after_second)} occurrences are intact")

    finally:
        if keep:
            print(f"\n--keep: leaving {summary} in place; delete it yourself.")
        else:
            print(f"\n4. Cleaning up {summary}")
            ev = store.get_event(uid, cal_id)
            if ev is not None:
                guard.check(ev)
                store.queue_delete(uid, cal_id)
                with __import__("contextlib").suppress(Exception):
                    await sync._tick(account, backend)
                print("   deleted")

    print()
    if failures:
        print(f"{_FAIL} {failures} check(s) failed")
    else:
        print(f"{_PASS} single-occurrence delete round-trips correctly")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", help="account id, display name, or identity")
    ap.add_argument("--list", action="store_true", help="list accounts and exit")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="required: confirms this may create and delete its own test events",
    )
    ap.add_argument("--all-day", action="store_true", help="use an all-day series")
    ap.add_argument("--keep", action="store_true", help="skip cleanup")
    args = ap.parse_args()

    if args.list:
        engine = open_engine(_default_db_path())
        with Session(engine) as s:
            for a in s.query(Account).all():
                print(f"{a.id}  {a.kind:12} {a.display_name}  {a.identity}")
        return 0

    if not args.account or not args.yes:
        ap.error("--account and --yes are both required (or use --list)")
    return asyncio.run(run(args.account, all_day=args.all_day, keep=args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
