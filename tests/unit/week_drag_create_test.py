"""Tests for WeekView's drag-to-create-event flow.

Drag-create on an empty grid is the entry point for every new-event flow
and broke once in ebe8750.  The harness drives it press → move → release,
patching `EventDialog` so the test never actually pops a dialog.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent

from tests.unit.conftest import empty_week_plan, make_fake_store


class _FakeDialog:
    """Stand-in for EventDialog that records constructor args and skips exec()."""

    last: "_FakeDialog | None" = None

    def __init__(self, parent, *, store, default_dt, default_dtend, default_all_day):
        self.parent = parent
        self.store = store
        self.default_dt = default_dt
        self.default_dtend = default_dtend
        self.default_all_day = default_all_day
        _FakeDialog.last = self

    def exec(self):
        return 0  # user "cancelled" → no queue_create call


@pytest.fixture
def patched_dialog(monkeypatch):
    """Patch EventDialog to the recording fake before _open_create_dialog imports it."""
    import lilical.ui.widgets.event_dialog as ed

    _FakeDialog.last = None
    monkeypatch.setattr(ed, "EventDialog", _FakeDialog)
    yield _FakeDialog


def _build_view(qapp):
    from lilical.ui.views.week import WeekView

    store = make_fake_store()
    view = WeekView(store, day_count=7, cal_info_provider=lambda: {})
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    view._apply_plan(empty_week_plan())
    qapp.processEvents()
    return view


def _press(view, vp_pt: QPoint) -> None:
    from PySide6.QtCore import QPointF

    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(vp_pt),
        QPointF(vp_pt),
        view.viewport().mapToGlobal(vp_pt).toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(ev)


def _move(view, vp_pt: QPoint) -> None:
    from PySide6.QtCore import QPointF

    ev = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(vp_pt),
        QPointF(vp_pt),
        view.viewport().mapToGlobal(vp_pt).toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseMoveEvent(ev)


def _release(view, vp_pt: QPoint) -> None:
    from PySide6.QtCore import QPointF

    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(vp_pt),
        QPointF(vp_pt),
        view.viewport().mapToGlobal(vp_pt).toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(ev)


def _body_point(view, *, vp_y: int = 400) -> QPoint:
    """A viewport point in the timed-body area of column 2."""
    from lilical.ui.views.week import TIME_AXIS_WIDTH

    body_w = view.viewport().width() - TIME_AXIS_WIDTH
    col_w = body_w / view._day_count
    x = int(TIME_AXIS_WIDTH + 2.5 * col_w)
    return QPoint(x, vp_y)


def test_drag_in_empty_area_renders_preview(qapp) -> None:
    """Press + move past the snap threshold renders a DragPreview."""
    view = _build_view(qapp)
    try:
        start = _body_point(view, vp_y=400)
        end = _body_point(view, vp_y=520)
        _press(view, start)
        _move(view, end)
        qapp.processEvents()

        assert view._drag_preview is not None, "drag preview was not created"
        r = view._drag_preview.boundingRect()
        assert r.width() > 0 and r.height() > 0, (
            f"drag preview has degenerate geometry: {r}"
        )
    finally:
        view._teardown_preview()
        view.close()
        view.deleteLater()


def test_release_after_threshold_opens_event_dialog(
    qapp, patched_dialog
) -> None:
    """Drag past snap threshold + release opens EventDialog with correct dt range."""
    view = _build_view(qapp)
    try:
        start = _body_point(view, vp_y=400)
        end = _body_point(view, vp_y=560)
        _press(view, start)
        _move(view, end)
        _release(view, end)
        qapp.processEvents()

        assert patched_dialog.last is not None, "EventDialog was never constructed"
        assert isinstance(patched_dialog.last.default_dt, datetime)
        assert isinstance(patched_dialog.last.default_dtend, datetime)
        assert patched_dialog.last.default_dtend > patched_dialog.last.default_dt
        assert patched_dialog.last.default_all_day is False
    finally:
        view._teardown_preview()
        view.close()
        view.deleteLater()


def test_click_without_drag_still_opens_dialog_for_default_hour(
    qapp, patched_dialog
) -> None:
    """A click (no drag) snaps to the press minute and opens a 1-hour dialog.

    mouseReleaseEvent treats `hi - lo < snap_minutes / 2` as a click and
    defaults to start_min..start_min+60 — same dialog path, default duration.
    """
    view = _build_view(qapp)
    try:
        pt = _body_point(view, vp_y=400)
        _press(view, pt)
        _release(view, pt)
        qapp.processEvents()

        assert patched_dialog.last is not None, (
            "EventDialog was not opened for a no-drag click"
        )
        dur = patched_dialog.last.default_dtend - patched_dialog.last.default_dt
        assert dur.total_seconds() == 3600, (
            f"no-drag click should default to 60 min, got {dur}"
        )
    finally:
        view._teardown_preview()
        view.close()
        view.deleteLater()
