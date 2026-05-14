from __future__ import annotations

from datetime import datetime

from lilical.models.event import Event


def resolve_conflict(
    local: Event,
    remote: Event,
) -> str:
    if local.sequence > remote.sequence:
        return "local"
    if remote.sequence > local.sequence:
        return "remote"
    if local.last_modified and remote.last_modified:
        if local.last_modified > remote.last_modified:
            return "local"
        return "remote"
    return "remote"
