
from lilical.models.event import Event


def _make_events(n: int) -> list[Event]:
    return [
        Event(
            uid=f"e{i}",
            calendar_id="cal-1",
            summary=f"Event {i}",
        )
        for i in range(n)
    ]


def test_event_creation() -> None:
    events = _make_events(3)
    assert len(events) == 3
    assert events[0].uid == "e0"
    assert events[1].uid == "e1"
    assert events[2].uid == "e2"


def test_event_is_immutable() -> None:
    e = Event(uid="e1", calendar_id="cal-1", summary="Original")
    import dataclasses
    e2 = dataclasses.replace(e, summary="Changed")
    assert e.summary == "Original"
    assert e2.summary == "Changed"
