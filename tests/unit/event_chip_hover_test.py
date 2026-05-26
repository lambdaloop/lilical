"""Tests for `EventChip.hovered` / `hover_left` signals.

These feed the right-side InspectorPane: hovering a chip should emit a
`PopoverEvent` payload plus the event's notes (or None) so the pane
can render details without consulting the underlying Event model.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QPointF, QRectF
from PySide6.QtWidgets import QGraphicsSceneHoverEvent

from lilical.models.event import Event
from lilical.ui.widgets._popover_rows import PopoverEvent
from lilical.ui.widgets.event_chip import ChipMode, EventChip


def _make_chip(*, all_day: bool = False, description: str = "") -> EventChip:
    today = datetime.now(tz=timezone.utc).date()
    start = datetime(today.year, today.month, today.day, 9, 0, tzinfo=timezone.utc)
    end = datetime(today.year, today.month, today.day, 10, 30, tzinfo=timezone.utc)
    ev = Event(
        uid="u1",
        calendar_id="cal-1",
        summary="Standup",
        location="Conf Room A",
        description=description,
        dtstart=start,
        dtend=end,
        all_day=all_day,
    )
    rect = QRectF(0, 0, 120, 40)
    return EventChip(
        ev,
        rect,
        calendar_color="#5e9fff",
        mode=ChipMode.TEXT,
        show_time_prefix=not all_day,
        time_prefix=None,
        time_format="24h",
        instance_dtstart=ev.dtstart,
        completed=False,
        inst_key=None,
        read_only=False,
    )


def _hover_enter(chip: EventChip) -> None:
    ev = QGraphicsSceneHoverEvent(QEvent.Type.GraphicsSceneHoverEnter)
    ev.setPos(QPointF(5, 5))
    chip.hoverEnterEvent(ev)


def _hover_leave(chip: EventChip) -> None:
    ev = QGraphicsSceneHoverEvent(QEvent.Type.GraphicsSceneHoverLeave)
    ev.setPos(QPointF(5, 5))
    chip.hoverLeaveEvent(ev)


def test_hover_enter_emits_hovered_with_popover_event(qapp) -> None:
    chip = _make_chip(description="discuss roadmap")
    payloads: list[tuple] = []
    chip.hovered.connect(lambda pe, notes: payloads.append((pe, notes)))

    _hover_enter(chip)
    assert len(payloads) == 1, "hovered should be emitted exactly once"
    pe, notes = payloads[0]
    assert isinstance(pe, PopoverEvent)
    assert pe.title == "Standup"
    assert pe.location == "Conf Room A"
    assert pe.uid == "u1"
    assert pe.calendar_color == "#5e9fff"
    assert notes == "discuss roadmap"


def test_hover_enter_time_str_contains_start_and_end(qapp) -> None:
    chip = _make_chip()
    payloads: list[tuple] = []
    chip.hovered.connect(lambda pe, notes: payloads.append((pe, notes)))

    _hover_enter(chip)
    pe, _ = payloads[0]
    # 09:00 UTC start, 10:30 UTC end. Local zone may shift; just check both
    # ends are 2-digit times separated by an en-dash.
    assert "–" in pe.time_str
    head, tail = [s.strip() for s in pe.time_str.split("–")]
    assert len(head) == 5 and head[2] == ":"
    assert len(tail) == 5 and tail[2] == ":"


def test_hover_enter_all_day_emits_all_day_label(qapp) -> None:
    chip = _make_chip(all_day=True)
    payloads: list[tuple] = []
    chip.hovered.connect(lambda pe, notes: payloads.append((pe, notes)))

    _hover_enter(chip)
    pe, _ = payloads[0]
    assert pe.time_str == "All day"


def test_hover_enter_with_no_description_emits_none_notes(qapp) -> None:
    chip = _make_chip(description="")
    payloads: list[tuple] = []
    chip.hovered.connect(lambda pe, notes: payloads.append((pe, notes)))

    _hover_enter(chip)
    _, notes = payloads[0]
    assert notes is None


def test_hover_leave_emits_hover_left(qapp) -> None:
    chip = _make_chip()
    count = [0]
    chip.hover_left.connect(lambda: count.__setitem__(0, count[0] + 1))

    _hover_leave(chip)
    assert count[0] == 1
