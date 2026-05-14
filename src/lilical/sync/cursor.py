from __future__ import annotations

from lilical.backends.base import SyncCursor


def cursor_from_json(data: dict | None) -> SyncCursor | None:
    if data is None:
        return None
    for cls in _CURSOR_REGISTRY:
        try:
            return cls.from_json(data)
        except Exception:
            continue
    return None


def cursor_to_json(cursor: SyncCursor | None) -> dict | None:
    if cursor is None:
        return None
    return cursor.to_json()


_CURSOR_REGISTRY: list[type[SyncCursor]] = []
