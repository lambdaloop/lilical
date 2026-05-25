from __future__ import annotations

from typing import Any

from lilical.backends.base import SyncCursor
from lilical.backends.caldav import CalDavCursor
from lilical.backends.google import GoogleCursor
from lilical.backends.graph import GraphCursor
from lilical.backends.subscription import SubscriptionCursor

_CURSORS: dict[str, type[SyncCursor]] = {
    GraphCursor._TYPE: GraphCursor,  # type: ignore[reportPrivateUsage]
    GoogleCursor._TYPE: GoogleCursor,  # type: ignore[reportPrivateUsage]
    CalDavCursor._TYPE: CalDavCursor,  # type: ignore[reportPrivateUsage]
    SubscriptionCursor._TYPE: SubscriptionCursor,  # type: ignore[reportPrivateUsage]
}


def cursor_from_json(data: dict[str, Any] | None) -> SyncCursor | None:
    if not data:
        return None
    cls = _CURSORS.get(data.get("_type", ""))
    if cls is None:
        return None  # untagged/legacy cursor — forces a one-time initial_sync
    return cls.from_json(data)


def cursor_to_json(cursor: SyncCursor | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    return cursor.to_json()
