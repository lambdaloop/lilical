import icalendar

from lilical.backends.caldav import _vevent_to_event


def test_simple_vevent() -> None:
    cal = icalendar.Calendar()
    cal.add("PRODID", "-//Test//EN")
    cal.add("VERSION", "2.0")
    ve = icalendar.Event()
    ve.add("UID", "test-uid@example.com")
    ve.add("SUMMARY", "Test event")
    ve.add("DESCRIPTION", "A description")
    ve.add("LOCATION", "Room 1")
    ve.add("SEQUENCE", 0)
    cal.add_component(ve)

    event = _vevent_to_event(ve, calendar_id="cal-1", href="/cal/evt.ics", etag='"abc"')
    assert event.uid == "test-uid@example.com"
    assert event.summary == "Test event"
    assert event.description == "A description"
    assert event.location == "Room 1"
    assert event.provider_event_id == "/cal/evt.ics"
    assert event.etag == '"abc"'


def test_vevent_defaults() -> None:
    ve = icalendar.Event()
    ve.add("UID", "minimal-uid")
    cal = icalendar.Calendar()
    cal.add_component(ve)

    event = _vevent_to_event(ve, calendar_id="cal-1", href="/min.ics", etag='"x"')
    assert event.uid == "minimal-uid"
    assert event.summary == ""
    assert event.description == ""
    assert event.location == ""
    assert event.sequence == 0
