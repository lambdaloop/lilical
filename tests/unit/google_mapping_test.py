from lilical.backends.google import _google_event_to_change


def test_timed_event() -> None:
    data = {
        "id": "evt123",
        "iCalUID": "uid-abc@google.com",
        "summary": "Lunch",
        "status": "confirmed",
        "etag": '"abc123"',
        "sequence": 1,
    }
    change = _google_event_to_change(data, "cal-1")
    assert change.kind == "upsert"
    assert change.uid == "uid-abc@google.com"
    assert change.event is not None
    assert change.event.summary == "Lunch"
    assert change.event.provider_event_id == "evt123"


def test_cancelled_event() -> None:
    data = {
        "id": "evt456",
        "iCalUID": "uid-xyz@google.com",
        "status": "cancelled",
    }
    change = _google_event_to_change(data, "cal-1")
    assert change.kind == "delete"
    assert change.uid == "uid-xyz@google.com"


def test_no_icaluid_falls_back_to_id() -> None:
    data = {
        "id": "evt789",
        "status": "confirmed",
    }
    change = _google_event_to_change(data, "cal-1")
    assert change.uid == "evt789"
