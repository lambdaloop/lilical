"""Unit tests for the InspectorPane widget."""

from __future__ import annotations

from lilical.ui.widgets._popover_rows import PopoverEvent
from lilical.ui.widgets.inspector_pane import InspectorPane


def _pe(
    title: str = "Standup",
    time_str: str = "09:00 – 09:30",
    location: str | None = None,
    color: str = "#5e9fff",
    uid: str | None = "u1",
) -> PopoverEvent:
    return PopoverEvent(
        time_str=time_str,
        title=title,
        location=location,
        calendar_color=color,
        uid=uid,
    )


def _shown(w) -> bool:
    """True if `w` was not explicitly hidden via setVisible(False).

    Use this in unit tests where the parent pane is never shown — Qt's
    isVisible() returns False for any descendant of an unshown parent, so
    isHidden() is the only way to read the widget's own visibility flag.
    """
    return not w.isHidden()


def test_clear_hides_all_sections(qapp) -> None:
    pane = InspectorPane()
    try:
        pane.clear()
        assert not _shown(pane._title)
        assert not _shown(pane._time)
        assert not _shown(pane._cluster_header)
        assert not _shown(pane._separator)
        assert pane._current_rows == []
    finally:
        pane.deleteLater()


def test_show_event_renders_title_time_location(qapp) -> None:
    pane = InspectorPane()
    try:
        pane.show_event(
            _pe(title="Sprint planning", time_str="10:00 – 11:30", location="Room B"),
            notes="discuss roadmap",
        )
        assert pane._title.text() == "Sprint planning"
        assert pane._time.text() == "10:00 – 11:30"
        assert "Room B" in pane._location.text()
        assert _shown(pane._location)
        assert "discuss roadmap" in pane._notes.text()
        assert _shown(pane._notes_header)
        # No cluster section when only show_event was called.
        assert not _shown(pane._cluster_header)
        assert not _shown(pane._separator)
        assert pane._current_rows == []
    finally:
        pane.deleteLater()


def test_show_event_with_no_location_hides_location_row(qapp) -> None:
    pane = InspectorPane()
    try:
        pane.show_event(_pe(location=None))
        assert not _shown(pane._location)
    finally:
        pane.deleteLater()


def test_show_event_with_no_notes_hides_notes(qapp) -> None:
    pane = InspectorPane()
    try:
        pane.show_event(_pe(), notes=None)
        assert not _shown(pane._notes_header)
        assert not _shown(pane._notes)
    finally:
        pane.deleteLater()


def test_show_cluster_renders_all_sibling_rows(qapp) -> None:
    pane = InspectorPane()
    try:
        primary = _pe(title="Code review", time_str="09:30 – 10:30", uid="u3")
        siblings = [
            _pe(title="Standup", time_str="09:00 – 09:30", uid="u1"),
            _pe(title="1:1", time_str="09:15 – 10:00", uid="u2"),
            primary,
        ]
        pane.show_cluster(primary, siblings)
        assert pane._title.text() == "Code review"
        assert _shown(pane._separator)
        assert _shown(pane._cluster_header)
        assert "3 EVENTS" in pane._cluster_header.text()
        assert len(pane._current_rows) == 3
    finally:
        pane.deleteLater()


def test_show_cluster_header_uses_first_and_last_times(qapp) -> None:
    pane = InspectorPane()
    try:
        siblings = [
            _pe(title="A", time_str="08:00 – 09:00", uid="a"),
            _pe(title="B", time_str="08:30 – 10:30", uid="b"),
        ]
        pane.show_cluster(siblings[0], siblings)
        header = pane._cluster_header.text()
        assert "08:00" in header
        assert "10:30" in header
        assert "2 EVENTS" in header
    finally:
        pane.deleteLater()


def test_show_cluster_then_show_event_clears_cluster_section(qapp) -> None:
    pane = InspectorPane()
    try:
        siblings = [
            _pe(title="A", time_str="09:00 – 10:00", uid="a"),
            _pe(title="B", time_str="09:30 – 10:30", uid="b"),
        ]
        pane.show_cluster(siblings[0], siblings)
        assert len(pane._current_rows) == 2

        pane.show_event(_pe(title="Solo", time_str="14:00 – 15:00", uid="z"))
        assert pane._title.text() == "Solo"
        assert not _shown(pane._cluster_header)
        assert not _shown(pane._separator)
        assert pane._current_rows == []
    finally:
        pane.deleteLater()


def test_show_event_after_clear_repopulates(qapp) -> None:
    pane = InspectorPane()
    try:
        pane.show_event(_pe(title="First"))
        pane.clear()
        pane.show_event(_pe(title="Second"))
        assert pane._title.text() == "Second"
        assert _shown(pane._title)
    finally:
        pane.deleteLater()
