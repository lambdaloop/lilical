from datetime import datetime, timezone

from lilical.models.event import Event
from lilical.sync.conflicts import resolve_conflict


def _event(uid: str, sequence: int = 0, last_modified: datetime | None = None) -> Event:
    return Event(
        uid=uid,
        calendar_id="cal-1",
        sequence=sequence,
        last_modified=last_modified or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_higher_sequence_wins() -> None:
    local = _event("e1", sequence=2)
    remote = _event("e1", sequence=1)
    assert resolve_conflict(local, remote) == "local"


def test_lower_sequence_loses() -> None:
    local = _event("e1", sequence=1)
    remote = _event("e1", sequence=3)
    assert resolve_conflict(local, remote) == "remote"


def test_same_sequence_newer_wins() -> None:
    may13 = datetime(2026, 5, 13, tzinfo=timezone.utc)
    may12 = datetime(2026, 5, 12, tzinfo=timezone.utc)
    local = _event("e1", sequence=1, last_modified=may13)
    remote = _event("e1", sequence=1, last_modified=may12)
    assert resolve_conflict(local, remote) == "local"


def test_same_sequence_older_loses() -> None:
    may13 = datetime(2026, 5, 13, tzinfo=timezone.utc)
    may12 = datetime(2026, 5, 12, tzinfo=timezone.utc)
    local = _event("e1", sequence=1, last_modified=may12)
    remote = _event("e1", sequence=1, last_modified=may13)
    assert resolve_conflict(local, remote) == "remote"
