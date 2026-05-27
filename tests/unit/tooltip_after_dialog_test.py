"""Regression tests for hover re-delivery after a modal dialog closes.

These exercise the full Qt event pipeline (QTest.mouseMove → QGraphicsView →
QGraphicsScene → hoverEnterEvent → hovered signal) to verify that
_refresh_hover_under_cursor() actually restores hover delivery.

The suite is intentionally instrumented: failing tests print diagnostic
state so we can see exactly what Qt is doing wrong.

NOTE: The tests that directly open QDialog() without a real click bypass the
production mouse-grab path and are only useful for exercising the
_refresh_hover_under_cursor helper in isolation.  The test
``test_hover_redelivers_after_real_chip_click`` is the authoritative
regression guard — it reproduces the complete production flow:
  QTest.mouseClick → scene implicit grab → mouseReleaseEvent →
  details_requested.emit → slot → dlg.exec() → … → hover restores.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QTimer
from PySide6.QtWidgets import QDialog

from lilical.ui.views.week import TIME_AXIS_WIDTH, WeekView
from lilical.ui.widgets.inspector_pane import InspectorPane
from tests.unit.conftest import (
    cluster_entry,
    cluster_event_data,
    empty_week_plan,
    make_event,
    make_fake_store,
    mouse_move_to,
    placement_entry,
)

# ─── helpers ──────────────────────────────────────────────────────────────────


def _build_view_with_chip(qapp, inspector):
    store = make_fake_store()
    view = WeekView(
        store, day_count=7, cal_info_provider=lambda: {}, inspector=inspector
    )
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()

    ev = make_event("solo", hour=10, minute=0, summary="Lone meeting")
    plan = empty_week_plan()
    plan["new_placements"] = {
        ("solo", 0): placement_entry(ev, QRectF(200, 600, 120, 60))
    }
    view._apply_plan(plan)
    qapp.processEvents()
    return view


def _build_view_with_cluster(qapp, inspector):
    store = make_fake_store()
    view = WeekView(
        store, day_count=7, cal_info_provider=lambda: {}, inspector=inspector
    )
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()

    events_data = []
    for i in range(3):
        s = 540 + i * 5
        e = s + 60
        events_data.append(
            cluster_event_data(
                make_event(f"c{i}", hour=9, minute=i * 5),
                start_min=s,
                end_min=e,
            )
        )
    plan = empty_week_plan()
    plan["new_cluster_placements"] = {
        ("cluster", 0, 540): cluster_entry(
            QRectF(TIME_AXIS_WIDTH, 540, 140, 120), events_data
        )
    }
    view._apply_plan(plan)
    qapp.processEvents()
    return view


def _scene_center(item) -> QPointF:
    return item.mapToScene(item.boundingRect().center())


def _open_and_close_dialog(qapp) -> None:
    dlg = QDialog()
    QTimer.singleShot(0, dlg.accept)
    dlg.exec()
    qapp.processEvents()


def _run_refresh(qapp) -> None:
    from lilical.ui.views._recurrence_actions import _refresh_hover_under_cursor

    _refresh_hover_under_cursor()
    qapp.processEvents()


# ─── diagnostic helper ────────────────────────────────────────────────────────


def _dump_qt_state(view, chip=None) -> str:
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication

    global_pos = QCursor.pos()
    widget_at = QApplication.widgetAt(global_pos)
    vp = view.viewport()
    vp_pt = vp.mapFromGlobal(global_pos)
    vp_contains = vp.rect().contains(vp_pt)
    scene = view.scene()
    item_at = (
        scene.itemAt(view.mapToScene(vp_pt), view.transform())
        if scene and vp_contains
        else None
    )
    hovered_flag = chip._hovered if chip else "n/a"
    return (
        f"  QCursor.pos()={global_pos.x()},{global_pos.y()}"
        f"  widgetAt={widget_at!r}"
        f"  vp_pt={vp_pt.x()},{vp_pt.y()}"
        f"  vp_contains={vp_contains}"
        f"  scene_item={item_at!r}"
        f"  chip._hovered={hovered_flag}"
    )


# ─── tests ────────────────────────────────────────────────────────────────────


def test_hover_fires_on_baseline_move(qapp) -> None:
    """Sanity: QTest.mouseMove to the chip actually triggers hoverEnterEvent."""
    inspector = InspectorPane()
    inspector.hide()
    view = _build_view_with_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        calls: list = []
        chip.hovered.connect(lambda *a: calls.append(a))

        mouse_move_to(view, _scene_center(chip), qapp)
        assert calls, (
            "baseline hover did not fire — mouse_move_to / scene positioning broken"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_hover_redelivers_after_modal_dialog_inspector_hidden(qapp) -> None:
    """Core regression: hover signal must re-fire after a QDialog.exec() cycle
    when the inspector is hidden (QToolTip fallback path).

    EXPECTED: passes once _refresh_hover_under_cursor is fixed.
    """
    inspector = InspectorPane()
    inspector.hide()
    view = _build_view_with_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        calls: list = []
        chip.hovered.connect(lambda *a: calls.append(a))

        # 1. Baseline hover.
        mouse_move_to(view, _scene_center(chip), qapp)
        assert calls, "baseline hover didn't fire — test setup broken"
        before_dialog = len(calls)

        # 2. Diagnostics: state before dialog.
        print("\n[before dialog]", _dump_qt_state(view, chip))

        # 3. Open + immediately close a real modal QDialog.
        _open_and_close_dialog(qapp)

        # 4. Diagnostics: state after dialog, before refresh.
        print("[after dialog, pre-refresh]", _dump_qt_state(view, chip))

        # 5. Simulate the real-desktop case: on Wayland/X11, hoverLeave is not
        #    always delivered when a modal opens, so the scene still considers
        #    the chip its current hovered item. Force that state here.
        chip._hovered = True

        # 6. Call production refresh helper.
        _run_refresh(qapp)

        # 7. Diagnostics: state after refresh.
        print("[after refresh]", _dump_qt_state(view, chip))
        print(f"  hover calls: before={before_dialog}, after={len(calls)}")

        assert len(calls) > before_dialog, (
            f"hovered did not re-fire after modal closed "
            f"(calls before dialog: {before_dialog}, after refresh: {len(calls)})"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_hover_redelivers_after_modal_dialog_inspector_visible(qapp) -> None:
    """Same flow with inspector visible — pane must re-populate."""
    inspector = InspectorPane()
    inspector.show()
    view = _build_view_with_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        calls: list = []
        chip.hovered.connect(lambda *a: calls.append(a))

        mouse_move_to(view, _scene_center(chip), qapp)
        assert calls, "baseline hover didn't fire — test setup broken"
        before_dialog = len(calls)

        _open_and_close_dialog(qapp)
        _run_refresh(qapp)

        assert len(calls) > before_dialog, (
            f"hovered did not re-fire with inspector visible "
            f"(before: {before_dialog}, after: {len(calls)})"
        )
        assert inspector._title.text() == "Lone meeting", (
            "inspector pane didn't re-populate after modal closed"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_single_synthetic_move_is_noop_when_chip_stays_hovered(qapp) -> None:
    """On real desktops Qt may NOT deliver hoverLeave when a modal opens, so the
    chip's _hovered flag and the scene's lastHoveredItem stay set. In that case,
    a synthetic MouseMove at the SAME position is a QGraphicsScene no-op: it
    doesn't re-deliver hoverEnter because the item didn't change.

    This test documents that single-move is unreliable and validates the fix:
    prefixing a Leave event clears the scene's hover tracking so the subsequent
    MouseMove always delivers a fresh hoverEnter.
    """
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    inspector = InspectorPane()
    inspector.hide()
    view = _build_view_with_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        calls: list = []
        chip.hovered.connect(lambda *a: calls.append(a))

        # 1. Move to chip — hover fires, chip is now "stuck hovered".
        mouse_move_to(view, _scene_center(chip), qapp)
        assert calls, "baseline hover didn't fire — test setup broken"

        # 2. Simulate real-desktop: chip stays hovered during modal.
        #    (In offscreen, Qt delivers hoverLeave automatically; here we force
        #     the "chip stayed hovered" scenario regardless of platform.)
        chip._hovered = True  # ensure stuck state

        # 3. Send ONLY a MouseMove at the same position (what the old single-move
        #    fix did). The scene should see "same item, no change" and not re-emit.
        vp = view.viewport()
        from PySide6.QtGui import QCursor
        global_pt = QCursor.pos()
        vp_pt = vp.mapFromGlobal(global_pt)
        move_only = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(vp_pt),
            QPointF(vp.mapToGlobal(vp_pt)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(vp, move_only)
        qapp.processEvents()
        after_single_move = len(calls)
        # (don't assert anything here — on offscreen this may or may not fire)

        # 4. Now apply the two-step fix: Leave clears scene hover state, then
        #    MouseMove re-delivers hoverEnter.
        chip._hovered = True  # re-stick it
        leave_ev = QEvent(QEvent.Type.Leave)
        QApplication.sendEvent(vp, leave_ev)
        move_again = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(vp_pt),
            QPointF(vp.mapToGlobal(vp_pt)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(vp, move_again)
        qapp.processEvents()

        # The two-step approach must always deliver a fresh hoverEnter.
        assert len(calls) > after_single_move, (
            "Leave + MouseMove did not re-deliver hoverEnter to the stuck-hovered chip"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_hover_redelivers_after_real_chip_click(qapp) -> None:
    """Authoritative regression test: reproduces the FULL production flow.

    QTest.mouseClick → scene implicit grab → mouseReleaseEvent emits
    details_requested synchronously → slot opens a real QDialog.exec() inside
    the event handler (nested event loop while the grab is in flight) → dialog
    auto-closes → all the stacks unwind → _refresh_hover_under_cursor fires →
    hover must be deliverable again.

    Previous tests bypassed the implicit-grab path by opening QDialog directly
    without a real chip click, which is why they passed in CI but the live app
    remained broken.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    inspector = InspectorPane()
    inspector.hide()
    view = _build_view_with_chip(qapp, inspector)
    try:
        chip = next(iter(view._chips.values()))
        hover_calls: list = []
        chip.hovered.connect(lambda *a: hover_calls.append(a))

        # 1. Baseline hover — must fire.
        mouse_move_to(view, _scene_center(chip), qapp)
        assert hover_calls, "baseline hover did not fire — test setup broken"

        # 2. Wire details_requested to a handler that opens a real QDialog
        #    (exactly what the production slot does) with an auto-close timer.
        #    Disconnect the view's own wiring first to avoid FakeStore.get_calendar
        #    being called from the production EventDetailsDialog path.
        chip.details_requested.disconnect()
        dialog_opened: list = []

        def _open_real_dialog(_event) -> None:
            dialog_opened.append(True)
            dlg = QDialog(view)
            QTimer.singleShot(0, dlg.accept)
            dlg.exec()
            # After dlg.exec() returns, schedule the production refresh helper
            # exactly as open_details_dialog does, but with the view hint.
            from lilical.ui.views._recurrence_actions import _refresh_hover_under_cursor
            QTimer.singleShot(0, lambda: _refresh_hover_under_cursor(view))

        chip.details_requested.connect(_open_real_dialog)

        before_click = len(hover_calls)

        # 3. Click the chip through the viewport — full press+release through
        #    the scene, taking the implicit mouse-grab path.
        vp_pt = view.mapFromScene(_scene_center(chip))
        QTest.mouseClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            vp_pt,
        )
        qapp.processEvents()

        # Sanity: the click should have triggered the dialog.
        assert dialog_opened, (
            "chip click did not reach details_requested slot — click missed the chip"
        )

        # 4. Dump diagnostic state.
        scene = view.scene()
        grabber = scene.mouseGrabberItem() if scene else None
        print("\n[real-click: after dialog closed]")
        print(f"  scene.mouseGrabberItem()={grabber!r}")
        print(_dump_qt_state(view, chip))

        # 5. Pump the timer so _refresh_hover_under_cursor fires.
        qapp.processEvents()
        print("[real-click: after refresh]")
        print(_dump_qt_state(view, chip))
        n = len(hover_calls)
        print(f"  hover calls: before_click={before_click}, after_refresh={n}")

        # 6. Now simulate the user hovering over the chip again (the move their
        #    mouse back over it after the dialog closes).
        mouse_move_to(view, _scene_center(chip), qapp)

        print("[real-click: after re-hover]")
        print(_dump_qt_state(view, chip))
        print(f"  hover calls total={len(hover_calls)}")

        # This is the authoritative assertion: hover MUST fire after the full
        # click→dialog→close flow.
        assert len(hover_calls) > before_click, (
            f"hovered did not fire after real chip click + dialog cycle "
            f"(before={before_click}, after={len(hover_calls)}, "
            f"grabber={grabber!r})"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()


def test_hover_redelivers_after_modal_dialog_cluster(qapp) -> None:
    """Cluster variant: hovered signal must re-fire after modal closes."""
    inspector = InspectorPane()
    inspector.hide()
    view = _build_view_with_cluster(qapp, inspector)
    try:
        cluster = next(iter(view._clusters.values()))
        calls: list = []
        cluster.hovered.connect(lambda *a: calls.append(a))

        mouse_move_to(view, _scene_center(cluster), qapp)
        assert calls, "baseline cluster hover didn't fire — test setup broken"
        before_dialog = len(calls)

        print("\n[cluster: before dialog]", _dump_qt_state(view))

        _open_and_close_dialog(qapp)

        print("[cluster: after dialog, pre-refresh]", _dump_qt_state(view))

        _run_refresh(qapp)

        print("[cluster: after refresh]", _dump_qt_state(view))
        print(f"  hover calls: before={before_dialog}, after={len(calls)}")

        assert len(calls) > before_dialog, (
            f"cluster hovered did not re-fire after modal closed "
            f"(before: {before_dialog}, after: {len(calls)})"
        )
    finally:
        view.close()
        view.deleteLater()
        inspector.deleteLater()
