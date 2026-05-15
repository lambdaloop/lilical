"""Diagnostic: query Graph directly and print event info for a given subject term.

Usage:
    pixi run -e dev python tools/diagnose_graph_series.py "Katie / Lili"

Loads the Graph account from the lilical DB + keyring (same path as the
running app) and prints:
  1. All events in the main Calendar matching the subject filter, with type,
     seriesMasterId, and recurrence info.
  2. Each referenced seriesMaster fetched individually via /me/events/{id}.
  3. The same masters fetched via $batch (mirrors production path).

No writes to DB or remote.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running from the project root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy.orm import Session

from lilical.backends.factory import build_backend_factory
from lilical.config import _default_db_path
from lilical.models.account import Account
from lilical.models.calendar import Calendar
from lilical.storage.db import open_engine
from lilical.storage.secrets import SecretsStore


def _short(s: str | None, n: int = 28) -> str:
    if not s:
        return "-"
    return s[-n:] if len(s) > n else s


async def main(term: str) -> None:
    db_path = _default_db_path()
    print(f"DB: {db_path}\n")

    engine = open_engine(db_path)
    with Session(engine) as s:
        accounts = s.query(Account).filter_by(kind="graph").all()
        if not accounts:
            print("No Graph accounts found in DB.")
            return
        account = accounts[0]
        print(f"Account: {account.display_name} ({account.identity})\n")
        cals = (
            s.query(Calendar).filter_by(account_id=account.id).all()
        )
        cal_map = {c.display_name: c for c in cals}

    secrets = SecretsStore()
    backend = build_backend_factory(secrets)(account)

    # Prefer the main "Calendar"; fall back to first calendar.
    main_cal = cal_map.get("Calendar") or cals[0]
    print(f"Searching calendar: {main_cal.display_name} ({main_cal.provider_id})\n")

    # ── Step 1: filter events by subject ──────────────────────────────────────
    print(f"=== Step 1: /events?$filter=contains(subject,'{term}') ===")
    # OData single-quote escaping: apostrophes in the value must be doubled.
    escaped_term = term.replace("'", "''")
    url = (
        f"/me/calendars/{main_cal.provider_id}/events"
        f"?$filter=contains(subject,'{escaped_term}')"
        f"&$select=id,subject,type,seriesMasterId,start,recurrence,isCancelled"
        f"&$top=100"
    )
    resp = await backend._request("GET", url)
    events = resp.json().get("value", [])
    print(f"  {len(events)} event(s) returned\n")

    master_ids: dict[str, str] = {}  # smi → subject of the referencing event
    for ev in events:
        ev_type = ev.get("type", "?")
        start = (ev.get("start") or {}).get("dateTime", "?")
        smi = ev.get("seriesMasterId") or ""
        cancelled = ev.get("isCancelled", False)
        subj = ev.get("subject", "")
        print(
            f"  type={ev_type:<16} "
            f"start={start[:19] if start != '?' else '?':<19} "
            f"cancelled={str(cancelled):<5} "
            f"smi={_short(smi):<28} "
            f"id={_short(ev.get('id'))} "
            f"subject={subj!r}"
        )
        if ev_type == "seriesMaster":
            rec = ev.get("recurrence")
            print(f"    recurrence={json.dumps(rec, indent=None)}")
        if smi:
            master_ids[smi] = subj

    if not master_ids:
        print(
            "\n  No seriesMasterId references found.\n"
            "  → H4 likely: events are singleInstance (not a true recurring series)\n"
            "    OR H1: the series is in a different calendar.\n"
        )
        print("  All calendars in this account:")
        for c in cals:
            print(f"    {c.display_name}: {c.id}")
        return

    # ── Step 2: fetch each master individually ─────────────────────────────────
    print(f"\n=== Step 2: /me/events/{{id}} for {len(master_ids)} master(s) ===")
    direct_ok: set[str] = set()
    for mid, ref_subj in master_ids.items():
        try:
            r = await backend._request("GET", f"/me/events/{mid}")
            m = r.json()
            m_type = m.get("type", "?")
            m_subj = m.get("subject", "")
            has_rec = m.get("recurrence") is not None
            print(
                f"  id={_short(mid)} "
                f"type={m_type:<16} "
                f"recurrence_present={has_rec} "
                f"subject={m_subj!r}"
            )
            if has_rec:
                print(f"    recurrence={json.dumps(m.get('recurrence'), indent=None)}")
            direct_ok.add(mid)
        except Exception as exc:
            print(f"  id={_short(mid)} FETCH FAILED: {exc!r}")

    # ── Step 3: same via $batch (mirrors production code) ─────────────────────
    print(f"\n=== Step 3: $batch fetch for {len(master_ids)} master(s) ===")
    fetched = await backend._graph_batch_get(list(master_ids))
    print(f"  $batch returned {len(fetched)}/{len(master_ids)} entries")
    for mid in master_ids:
        got = fetched.get(mid)
        if got is None:
            in_direct = mid in direct_ok
            print(
                f"  id={_short(mid)} MISSING from $batch "
                f"(direct GET {'succeeded' if in_direct else 'also failed'})"
            )
        else:
            b_type = got.get("type", "?")
            has_rec = got.get("recurrence") is not None
            print(
                f"  id={_short(mid)} "
                f"type={b_type:<16} "
                f"recurrence_present={has_rec}"
            )

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    all_types_step1 = {ev.get("type") for ev in events}
    singleinstance_only = all_types_step1 == {"singleInstance"}
    batch_ok_count = sum(1 for mid in master_ids if mid in fetched)
    batch_seriesmaster_count = sum(
        1
        for mid, m in fetched.items()
        if str(m.get("type") or "").lower() == "seriesmaster"
    )

    if singleinstance_only:
        print("  H4 (likely): all events are singleInstance — no true recurring series.")
    elif not master_ids:
        print("  H1 (possible): no occurrences/exceptions referencing a master.")
    elif batch_ok_count < len(master_ids):
        print(
            f"  H2 (likely): $batch returned only {batch_ok_count}/{len(master_ids)} "
            f"masters — throttling or 404?"
        )
    elif batch_seriesmaster_count < len(fetched):
        print(
            f"  H3 (likely): {len(fetched) - batch_seriesmaster_count} master(s) "
            f"returned wrong type from $batch — inject guard drops them."
        )
    else:
        print("  Unclear — all masters fetched correctly with seriesMaster type.")
        print("  Check whether occurrences/exceptions are present in step 1 output.")


async def check_delta_recurrence(term: str) -> None:
    """Paginate through calendarView/delta and print recurrence field presence for matches."""
    db_path = _default_db_path()
    engine = open_engine(db_path)
    with Session(engine) as s:
        account = s.query(Account).filter_by(kind="graph").first()
        cal = s.query(Calendar).filter_by(account_id=account.id, display_name="Calendar").first()

    secrets = SecretsStore()
    backend = build_backend_factory(secrets)(account)

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (now + datetime.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%S")
    next_url: str | None = (
        f"/me/calendars/{cal.provider_id}/calendarView/delta"
        f"?startDateTime={start}&endDateTime={end}"
    )

    print(f"\n=== Step 4: paginate calendarView/delta for seriesMasters matching '{term}' ===")
    total_events = 0
    page = 0
    all_matches: list[dict] = []
    while next_url:
        page += 1
        resp = await backend._request("GET", next_url)
        data = resp.json()
        events = data.get("value", [])
        total_events += len(events)
        matches = [
            e for e in events
            if term.lower() in str(e.get("subject", "")).lower()
            and str(e.get("type", "")).lower() == "seriesmaster"
        ]
        all_matches.extend(matches)
        if matches:
            print(f"  Page {page}: {len(events)} events, {len(matches)} match(es)")
        next_url = data.get("@odata.nextLink")
        if not next_url:
            break  # stop at delta link (don't follow the delta)
    print(f"  Total events scanned: {total_events} across {page} page(s)")
    print(f"  seriesMaster matches: {len(all_matches)}")
    for ev in all_matches:
        has_rec = ev.get("recurrence") is not None
        print(
            f"  id={_short(ev.get('id')):<28} "
            f"start={(ev.get('start') or {}).get('dateTime', '?')[:19]} "
            f"recurrence_present={has_rec} "
            f"subject={ev.get('subject')!r}"
        )
        if has_rec:
            print(f"    recurrence={json.dumps(ev.get('recurrence'), indent=None)}")
        else:
            print(f"    *** recurrence field IS ABSENT from calendarView/delta response! ***")


async def check_delta_exceptions(master_ids_of_interest: set[str]) -> None:
    """Paginate calendarView/delta and report any exceptions for the given master IDs."""
    db_path = _default_db_path()
    engine = open_engine(db_path)
    with Session(engine) as s:
        account = s.query(Account).filter_by(kind="graph").first()
        cal = s.query(Calendar).filter_by(account_id=account.id, display_name="Calendar").first()

    secrets = SecretsStore()
    backend = build_backend_factory(secrets)(account)

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    end = (now + datetime.timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%S")
    next_url: str | None = (
        f"/me/calendars/{cal.provider_id}/calendarView/delta"
        f"?startDateTime={start}&endDateTime={end}"
    )

    print(f"\n=== Step 5: exceptions/occurrences for known master IDs in delta ===")
    total_events = 0
    page = 0
    found_exceptions = []
    # Also track ordering of master vs exceptions on same page
    page_items: dict[int, list[dict]] = {}
    while next_url:
        page += 1
        resp = await backend._request("GET", next_url)
        data = resp.json()
        events = data.get("value", [])
        total_events += len(events)
        for i, ev in enumerate(events):
            ev_type = str(ev.get("type") or "").lower()
            ev_id = ev.get("id") or ""
            smi = ev.get("seriesMasterId") or ""
            # Check if this event's own id or seriesMasterId is one of our targets
            interesting = (
                any(ev_id.endswith(suffix) for suffix in master_ids_of_interest)
                or any(smi.endswith(suffix) for suffix in master_ids_of_interest)
            )
            if interesting:
                orig = ev.get("originalStart") or {}
                info = {
                    "page": page,
                    "page_idx": i,
                    "type": ev_type,
                    "id": _short(ev_id),
                    "smi": _short(smi),
                    "start": (ev.get("start") or {}).get("dateTime", "?"),
                    "orig_start": orig.get("dateTime"),
                    "orig_tz": orig.get("timeZone"),
                    "rrule_present": ev.get("recurrence") is not None,
                    "subject": ev.get("subject", ""),
                    "cancelled": ev.get("isCancelled", False),
                }
                found_exceptions.append(info)
        next_url = data.get("@odata.nextLink")
        if not next_url:
            break

    print(f"  Scanned {total_events} events across {page} pages")
    print(f"  Found {len(found_exceptions)} event(s) referencing target masters:")
    for e in found_exceptions:
        print(
            f"  page={e['page']} idx={e['page_idx']:3d} "
            f"type={e['type']:<16} "
            f"id={e['id']:<28} "
            f"smi={e['smi']:<28} "
            f"rrule_present={e['rrule_present']} "
            f"cancelled={e['cancelled']} "
            f"start={str(e['start'])[:19]} "
            f"orig={e['orig_start']} ({e['orig_tz']}) "
            f"subj={e['subject']!r}"
        )


if __name__ == "__main__":
    term = sys.argv[1] if len(sys.argv) > 1 else "Katie"
    asyncio.run(main(term))
    asyncio.run(check_delta_recurrence(term))
    # The 4 broken masters (IDs end with these suffixes)
    broken_masters = {
        "ZLnURYb5iyfXpfjPAABzuVikAAA=",  # Wed 2025-10-29
        "ZLnURYb5iyfXpfjPAADsR5XaAAA=",  # Wed 2026-04-29
        "ZLnURYb5iyfXpfjPAAB8gdHQAAA=",  # Fri 2025-11-21
        "ZLnURYb5iyfXpfjPAACXrwyMAAA=",  # Fri 2026-01-09
    }
    asyncio.run(check_delta_exceptions(broken_masters))
