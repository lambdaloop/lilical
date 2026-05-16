from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal
from sqlalchemy.orm import Session

from lilical.models.contact import SOURCE_PRIORITY, Contact, ContactRow, ContactsSyncStateRow


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContactStore(QObject):
    contacts_changed = Signal(str)  # account_id

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self._engine = engine
        self._write_lock = threading.RLock()

    def upsert_many(
        self, account_id: str, source: str, contacts: list[Contact]
    ) -> None:
        if not contacts:
            return
        now = _utc_now()
        with threading.RLock(), Session(self._engine) as s, s.begin():
            for c in contacts:
                row = (
                    s.query(ContactRow)
                    .filter_by(account_id=account_id, source=source, email=c.email.lower())
                    .first()
                )
                if row is None:
                    s.add(
                        ContactRow(
                            account_id=account_id,
                            source_id=c.source_id,
                            email=c.email.lower(),
                            display_name=c.display_name,
                            source=source,
                            last_seen_at=now,
                            inserted_at=now,
                        )
                    )
                else:
                    if c.source_id:
                        row.source_id = c.source_id
                    if c.display_name:
                        row.display_name = c.display_name
                    row.last_seen_at = now
        self.contacts_changed.emit(account_id)

    def upsert_harvested(
        self, account_id: str, pairs: list[tuple[str, str | None]]
    ) -> None:
        """Upsert (email, display_name) pairs with source='harvested'.

        Only overwrites display_name when no higher-priority source already has
        this email. Never downgrades source priority.
        """
        if not pairs:
            return
        now = _utc_now()
        harvested_priority = SOURCE_PRIORITY["harvested"]
        with threading.RLock(), Session(self._engine) as s, s.begin():
            for email_raw, display_name in pairs:
                email = email_raw.lower()
                if not email or "@" not in email:
                    continue
                # Check if a higher-priority source already has this email.
                existing = (
                    s.query(ContactRow)
                    .filter_by(account_id=account_id, email=email)
                    .all()
                )
                if existing:
                    for row in existing:
                        row.last_seen_at = now
                        if row.source == "harvested" and display_name and not row.display_name:
                            row.display_name = display_name
                else:
                    s.add(
                        ContactRow(
                            account_id=account_id,
                            source="harvested",
                            email=email,
                            display_name=display_name,
                            last_seen_at=now,
                            inserted_at=now,
                        )
                    )
        _ = harvested_priority  # used conceptually above

    def search(
        self,
        prefix: str,
        account_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[Contact]:
        prefix_lower = prefix.lower()
        with Session(self._engine) as s:
            q = s.query(ContactRow)
            if account_ids is not None:
                q = q.filter(ContactRow.account_id.in_(account_ids))
            rows = q.all()

        # Filter and rank in Python (SQLite LIKE is enough for the small dataset).
        matches: list[tuple[int, str, ContactRow]] = []
        for row in rows:
            email_match = row.email.startswith(prefix_lower)
            name_match = (row.display_name or "").lower().startswith(prefix_lower)
            if not email_match and not name_match:
                # Also try any word in the name.
                words = (row.display_name or "").lower().split()
                name_match = any(w.startswith(prefix_lower) for w in words)
            if email_match or name_match:
                priority = SOURCE_PRIORITY.get(row.source, 99)
                matches.append((priority, row.last_seen_at or "", row))

        matches.sort(key=lambda t: (t[0], t[1]), reverse=False)
        # Deduplicate by email (keep highest priority entry).
        seen_emails: set[str] = set()
        out: list[Contact] = []
        for _, _, row in matches:
            if row.email in seen_emails:
                continue
            seen_emails.add(row.email)
            out.append(
                Contact(
                    email=row.email,
                    display_name=row.display_name,
                    source=row.source,
                    account_id=row.account_id,
                    source_id=row.source_id,
                    last_seen_at=row.last_seen_at,
                )
            )
            if len(out) >= limit:
                break
        return out

    def get_sync_state(self, account_id: str, source: str) -> ContactsSyncStateRow | None:
        with Session(self._engine) as s:
            return (
                s.query(ContactsSyncStateRow)
                .filter_by(account_id=account_id, source=source)
                .first()
            )

    def needs_refresh(
        self, account_id: str, source: str, *, hours: float = 24
    ) -> bool:
        state = self.get_sync_state(account_id, source)
        if state is None or not state.last_full_refresh_at:
            return True
        from datetime import timedelta

        try:
            last = datetime.fromisoformat(state.last_full_refresh_at)
            return (datetime.now(timezone.utc) - last) > timedelta(hours=hours)
        except ValueError:
            return True

    def set_sync_state(
        self, account_id: str, source: str, cursor: str | None, *, mark_refreshed: bool = False
    ) -> None:
        now = _utc_now()
        with threading.RLock(), Session(self._engine) as s, s.begin():
            row = (
                s.query(ContactsSyncStateRow)
                .filter_by(account_id=account_id, source=source)
                .first()
            )
            if row is None:
                row = ContactsSyncStateRow(account_id=account_id, source=source)
                s.add(row)
            row.cursor = cursor
            if mark_refreshed:
                row.last_full_refresh_at = now
