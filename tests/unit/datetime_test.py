from datetime import datetime, timezone

import pytest

from lilical.models.event import Event


def _check_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime: {dt!r}")
    return dt


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def test_utc_now_returns_aware() -> None:
    now = utc_now()
    assert now.tzinfo is not None


def test_check_aware_accepts_aware() -> None:
    dt = datetime(2026, 5, 13, tzinfo=timezone.utc)
    _check_aware(dt)


def test_check_aware_rejects_naive() -> None:
    dt = datetime(2026, 5, 13)
    with pytest.raises(ValueError):
        _check_aware(dt)


def test_event_defaults() -> None:
    e = Event(uid="test-uid", calendar_id="test-cal")
    assert e.uid == "test-uid"
    assert e.calendar_id == "test-cal"
    assert e.summary == ""
    assert e.all_day is False
    assert e.status == "CONFIRMED"
    assert e.transparency == "OPAQUE"
