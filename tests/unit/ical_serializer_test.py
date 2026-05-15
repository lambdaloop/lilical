"""Tests for _ical_serializer.event_to_vcalendar and helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from lilical.backends._ical_serializer import _add_dt_with_tzid, event_to_vcalendar
from lilical.models.event import Event


def test_optional_fields_serialized():
    """description, location, url, color all produce VEVENT properties."""
    event = Event(
        uid="uid-opt",
        calendar_id="cal-1",
        summary="Optional fields",
        description="A desc",
        location="Room 42",
        url="https://example.com",
        color="#ff0000",
        dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    )
    cal = event_to_vcalendar(event)
    ve = cal.subcomponents[0]
    assert str(ve.get("description")) == "A desc"
    assert str(ve.get("location")) == "Room 42"
    assert str(ve.get("url")) == "https://example.com"
    assert str(ve.get("color")) == "#ff0000"


def test_exdate_and_rdate_serialized():
    """exdates and rdates each produce VEVENT properties."""
    exdate = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    rdate = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    event = Event(
        uid="uid-exrd",
        calendar_id="cal-1",
        summary="ER",
        rrule="FREQ=DAILY;COUNT=3",
        exdates=(exdate,),
        rdates=(rdate,),
        dtstart=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    )
    cal = event_to_vcalendar(event)
    ve = cal.subcomponents[0]
    exdate_props = ve.subcomponents if hasattr(ve, "subcomponents") else []
    exdates = ve.get("exdate")
    rdates = ve.get("rdate")
    assert exdates is not None
    assert rdates is not None


def test_recurrence_id_serialized():
    """Overrides get a RECURRENCE-ID property."""
    event = Event(
        uid="uid-rec-id",
        calendar_id="cal-1",
        summary="Override",
        recurrence_id=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        dtstart=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        dtend=datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc),
    )
    cal = event_to_vcalendar(event)
    ve = cal.subcomponents[0]
    rid = ve.get("recurrence-id")
    assert rid is not None


def test_add_dt_with_tzid_invalid_tz_falls_back_to_utc():
    """_add_dt_with_tzid falls back to UTC when the timezone is invalid."""
    import icalendar
    from datetime import timedelta, timezone as dt_tz

    ve = icalendar.Event()
    # Use a non-UTC timezone so we actually hit the try/except block
    ny_tz = dt_tz(timedelta(hours=-5), "EST")
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=ny_tz)
    _add_dt_with_tzid(ve, "dtstart", dt, "America/InvalidZone")
    prop = ve.get("dtstart")
    assert prop is not None
    assert prop.dt.tzinfo == timezone.utc


def test_add_dt_with_tzid_utc_stays_utc():
    """When tz_name is UTC, DTSTART is stored as plain UTC."""
    import icalendar

    ve = icalendar.Event()
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    _add_dt_with_tzid(ve, "dtstart", dt, "UTC")
    prop = ve.get("dtstart")
    assert prop is not None
    assert prop.dt.tzinfo == timezone.utc


def test_add_dt_with_tzid_empty_tz_name_falls_back():
    """When tz_name is empty, the datetime is stored as UTC."""
    import icalendar

    ve = icalendar.Event()
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    _add_dt_with_tzid(ve, "dtstart", dt, "")
    prop = ve.get("dtstart")
    assert prop is not None
    assert prop.dt.tzinfo == timezone.utc
