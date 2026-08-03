"""Tests for the toolbar timezone picker.

The pure helpers carry most of the behaviour and need no QApplication; the
widget tests cover the parts that only exist once Qt is involved (fixed width,
elision, signal discipline, and the popup's search-driven filtering).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lilical.ui.widgets.timezone_picker import (
    filter_zones,
    zone_abbrev,
    zone_button_label,
    zone_city,
    zone_offset_label,
    zone_row_label,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_LA = "America/Los_Angeles"
_TOKYO = "Asia/Tokyo"
_ZONES = [
    _LA,
    _TOKYO,
    "America/Denver",
    "America/Argentina/Buenos_Aires",
    "Asia/Kolkata",
    "Europe/Berlin",
    "Pacific/Auckland",
    "US/Pacific",
    "Etc/GMT+5",
    "UTC",
]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


# ── Pure helpers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (_LA, "Los Angeles"),
        ("America/Argentina/Buenos_Aires", "Buenos Aires"),
        ("UTC", "UTC"),
    ],
)
def test_zone_city_strips_region_and_underscores(name, expected) -> None:
    assert zone_city(name) == expected


def test_zone_abbrev_returns_alpha_abbrev() -> None:
    july = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("UTC"))
    assert zone_abbrev("America/Chicago", july) == "CDT"


def test_zone_abbrev_falls_back_to_offset_for_numeric_zones() -> None:
    """Etc/GMT+5 reports "-05" as its %Z, which reads as noise on a button."""
    july = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("UTC"))
    assert zone_abbrev("Etc/GMT+5", july) == "UTC−05:00"


def test_zone_offset_label_formats_half_hour_zones() -> None:
    july = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("UTC"))
    assert zone_offset_label("Asia/Kolkata", july) == "UTC+05:30"


def test_zone_button_label_format() -> None:
    july = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("UTC"))
    assert zone_button_label(_LA, july) == "Los Angeles · PDT"


def test_zone_row_label_shows_city_and_full_name() -> None:
    assert zone_row_label(_LA) == "Los Angeles — America/Los_Angeles"
    assert zone_row_label("UTC") == "UTC"


def test_filter_zones_exact_city_ranks_first() -> None:
    assert filter_zones("denver", _ZONES)[0] == "America/Denver"


def test_filter_zones_matches_across_slash() -> None:
    assert filter_zones("america los", _ZONES)[0] == _LA


def test_filter_zones_underscore_and_space_equivalent() -> None:
    assert filter_zones("buenos_aires", _ZONES) == filter_zones("buenos aires", _ZONES)


def test_filter_zones_blank_query_pins_current_then_system() -> None:
    out = filter_zones("", _ZONES, current=_TOKYO, system=_LA)
    assert out[:2] == [_TOKYO, _LA]
    assert len(out) == len(_ZONES)


def test_filter_zones_demotes_legacy_aliases() -> None:
    """Canonical Region/City should beat the US/* alias."""
    out = filter_zones("pacific", _ZONES)
    assert out.index("Pacific/Auckland") < out.index("US/Pacific")


def test_filter_zones_abbrev_match() -> None:
    out = filter_zones("pdt", _ZONES, abbrevs={_LA: "PDT", _TOKYO: "JST"})
    assert out == [_LA]


def test_filter_zones_offset_match() -> None:
    out = filter_zones("+05:30", _ZONES, offsets={"Asia/Kolkata": 330, _LA: -420})
    assert out == ["Asia/Kolkata"]


def test_filter_zones_offset_match_negative() -> None:
    out = filter_zones("utc-7", _ZONES, offsets={"Asia/Kolkata": 330, _LA: -420})
    assert out == [_LA]


def test_filter_zones_respects_limit() -> None:
    assert len(filter_zones("a", _ZONES, limit=3)) <= 3


def test_filter_zones_no_match_is_empty() -> None:
    assert filter_zones("zzzzz", _ZONES) == []


# ── Widget ─────────────────────────────────────────────────────────────────


def _picker(qapp):
    from lilical.ui.widgets.timezone_picker import TimezonePicker

    return TimezonePicker()


def test_picker_width_is_bounded_but_shrinkable(qapp) -> None:
    """Capped so it can't grow the window; shrinkable so it can't push the
    inspector and settings buttons into QToolBar's ">>" overflow."""
    p = _picker(qapp)
    try:
        assert p.maximumWidth() == 110
        assert p.minimumWidth() == 62
        assert p.sizeHint().width() == 110
    finally:
        p.deleteLater()


def test_picker_falls_back_to_abbrev_when_squeezed(qapp) -> None:
    """A squeezed button shows "PDT", not a mid-word "Los Angeles · P…"."""
    p = _picker(qapp)
    try:
        p.set_zone_name("America/Los_Angeles")
        p.resize(62, p.height())
        p.refresh_label()
        assert p.text() == zone_abbrev("America/Los_Angeles")
        assert "…" not in p.text()
        assert "America/Los_Angeles" in p.toolTip()
    finally:
        p.deleteLater()


def test_picker_shows_full_label_when_it_fits(qapp) -> None:
    p = _picker(qapp)
    try:
        p.set_zone_name("Asia/Tokyo")
        p.resize(110, p.height())
        p.refresh_label()
        assert p.text() == zone_button_label("Asia/Tokyo")
    finally:
        p.deleteLater()


def test_picker_label_updates_after_set_zone_name(qapp) -> None:
    p = _picker(qapp)
    try:
        p.set_zone_name(_TOKYO)
        assert p.zone_name() == _TOKYO
        assert "Tokyo" in p.text() or p.text().endswith("…")
        assert _TOKYO in p.toolTip()
    finally:
        p.deleteLater()


def test_set_zone_name_does_not_emit(qapp) -> None:
    """MainWindow calls this to sync the button; re-emitting would loop."""
    p = _picker(qapp)
    seen = []
    try:
        p.zone_changed.connect(seen.append)
        p.set_zone_name(_TOKYO)
        assert seen == []
    finally:
        p.deleteLater()


def test_set_zone_name_ignores_unknown_zone(qapp) -> None:
    p = _picker(qapp)
    try:
        before = p.zone_name()
        p.set_zone_name("Not/AZone")
        assert p.zone_name() == before
    finally:
        p.deleteLater()


def test_popup_search_filters_list(qapp) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    p = _picker(qapp)
    try:
        p._open_popup()
        qapp.processEvents()
        popup = p._popup
        assert popup is not None
        # The search field must hold real keyboard focus for typing to work.
        assert popup._search.hasFocus()

        QTest.keyClicks(popup._search, "tokyo")
        qapp.processEvents()
        assert popup._results.count() >= 1
        first = popup._results.item(0)
        assert first.data(Qt.ItemDataRole.UserRole) == _TOKYO
    finally:
        if p._popup is not None:
            p._popup.close()
        p.deleteLater()


def test_selecting_row_emits_zone_changed(qapp) -> None:
    p = _picker(qapp)
    seen = []
    try:
        p.set_zone_name(_LA)
        p.zone_changed.connect(seen.append)
        p._open_popup()
        qapp.processEvents()
        popup = p._popup
        assert popup is not None
        popup._search.setText("tokyo")
        qapp.processEvents()
        popup._commit_current()
        qapp.processEvents()
        assert seen == [_TOKYO]
        assert p.zone_name() == _TOKYO
    finally:
        if p._popup is not None:
            p._popup.close()
        p.deleteLater()


def test_picking_the_current_zone_does_not_emit(qapp) -> None:
    p = _picker(qapp)
    seen = []
    try:
        p.set_zone_name(_TOKYO)
        p.zone_changed.connect(seen.append)
        p._on_picked(_TOKYO)
        assert seen == []
    finally:
        p.deleteLater()


def test_filter_zones_explicit_alias_still_findable() -> None:
    """Demoting aliases must not make them unreachable when named directly."""
    assert filter_zones("us/pacific", _ZONES) == ["US/Pacific"]
    assert filter_zones("us pacific", _ZONES) == ["US/Pacific"]
