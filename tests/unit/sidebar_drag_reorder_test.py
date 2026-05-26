"""Tests for Sidebar drag-and-drop reordering of accounts and calendars.

`Sidebar.dragEnterEvent`/`dragMoveEvent`/`dropEvent` consume custom mime
types (`application/x-lilical-account-drag`,
`application/x-lilical-calendar-drag` + `application/x-lilical-account-id`)
to reorder accounts and within-account calendars.

Driving a real OS-level `QDrag.exec()` headlessly is brittle.  The fallback
used here (and in Qt's own offscreen test suite) is to construct
`QDragEnterEvent` / `QDragMoveEvent` / `QDropEvent` directly and call the
handler methods.  This covers the reorder logic and the
`account_order_changed`/`calendar_order_changed` emissions without
exercising the OS drag pipeline.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData, QPoint, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from tests.unit.conftest import make_fake_store


def _make_cal_info(acc_id: str, cal_id: str, name: str, color: str = "#3498db"):
    from lilical.ui.main_window import CalInfo

    return CalInfo(
        id=cal_id,
        display_name=name,
        color=color,
        account_id=acc_id,
        visible=True,
        read_only=False,
    )


def _build_sidebar(qapp):
    from lilical.ui.sidebar import Sidebar

    cal_info = {
        "c1a": _make_cal_info("acc-1", "c1a", "Work"),
        "c1b": _make_cal_info("acc-1", "c1b", "Personal"),
        "c2a": _make_cal_info("acc-2", "c2a", "Family"),
        "c2b": _make_cal_info("acc-2", "c2b", "Sports"),
    }
    account_meta = {
        "acc-1": ("Work account", "work@example.com", "google"),
        "acc-2": ("Home account", "home@example.com", "icloud"),
    }
    store = make_fake_store()

    # Record store.set_*_orders calls so the test can assert against them.
    captured: dict = {"account": None, "calendar": None}

    def set_account_orders(orders):
        captured["account"] = list(orders)

    def set_calendar_orders(orders):
        captured["calendar"] = list(orders)

    store.set_account_orders = set_account_orders  # type: ignore[method-assign]
    store.set_calendar_orders = set_calendar_orders  # type: ignore[method-assign]

    sidebar = Sidebar(
        store,
        cal_info_provider=lambda: cal_info,
        account_meta_provider=lambda: account_meta,
    )
    sidebar.resize(220, 600)
    sidebar.show()
    qapp.processEvents()
    return sidebar, captured


def _drag_enter(sidebar, mime: QMimeData, pos: QPoint | None = None) -> None:
    if pos is None:
        pos = QPoint(50, 50)
    ev = QDragEnterEvent(
        pos,
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    sidebar.dragEnterEvent(ev)


def _drop(sidebar, mime: QMimeData, pos: QPoint) -> None:
    ev = QDropEvent(
        pos.toPointF(),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    sidebar.dropEvent(ev)


def _account_mime(account_id: str) -> QMimeData:
    m = QMimeData()
    m.setData(
        "application/x-lilical-account-drag",
        QByteArray(account_id.encode()),
    )
    return m


def _calendar_mime(cal_id: str, account_id: str) -> QMimeData:
    m = QMimeData()
    m.setData(
        "application/x-lilical-calendar-drag",
        QByteArray(cal_id.encode()),
    )
    m.setData(
        "application/x-lilical-account-id",
        QByteArray(account_id.encode()),
    )
    return m


def test_drag_enter_account_sets_drag_state(qapp) -> None:
    """dragEnterEvent on an account-drag mime sets the internal drag kind/info."""
    sidebar, _ = _build_sidebar(qapp)
    try:
        mime = _account_mime("acc-1")
        _drag_enter(sidebar, mime)
        assert sidebar._drag_active is True
        assert sidebar._drag_kind == "account"
        assert sidebar._drag_info["source_id"] == "acc-1"
    finally:
        sidebar._stop_auto_scroll()
        sidebar.close()
        sidebar.deleteLater()


def test_drag_enter_calendar_sets_drag_state(qapp) -> None:
    """dragEnterEvent on a calendar-drag mime records the source cal + account."""
    sidebar, _ = _build_sidebar(qapp)
    try:
        mime = _calendar_mime("c1b", "acc-1")
        _drag_enter(sidebar, mime)
        assert sidebar._drag_kind == "calendar"
        assert sidebar._drag_info["source_id"] == "c1b"
        assert sidebar._drag_info["source_account_id"] == "acc-1"
    finally:
        sidebar._stop_auto_scroll()
        sidebar.close()
        sidebar.deleteLater()


def test_account_drop_emits_account_order_changed(qapp) -> None:
    """Dropping acc-1 below acc-2 emits account_order_changed with new order."""
    sidebar, captured = _build_sidebar(qapp)
    try:
        received: list = []
        sidebar.account_order_changed.connect(lambda: received.append(True))

        mime = _account_mime("acc-1")
        _drag_enter(sidebar, mime)
        # Force drop index to "after acc-2" → insert at the very end.
        sidebar._drop_insert_idx = 2
        _drop(sidebar, mime, QPoint(50, 500))
        qapp.processEvents()

        assert received == [True], (
            f"account_order_changed should fire once, got {received}"
        )
        assert captured["account"] is not None
        order = [aid for aid, _ in captured["account"]]
        assert order == ["acc-2", "acc-1"], (
            f"unexpected account order after drop: {order}"
        )
    finally:
        sidebar._stop_auto_scroll()
        sidebar.close()
        sidebar.deleteLater()


def test_calendar_drop_emits_calendar_order_changed(qapp) -> None:
    """Dropping calendar c1a below c1b within acc-1 emits calendar_order_changed."""
    sidebar, captured = _build_sidebar(qapp)
    try:
        received: list[str] = []
        sidebar.calendar_order_changed.connect(received.append)

        mime = _calendar_mime("c1a", "acc-1")
        _drag_enter(sidebar, mime)
        # Drop index "after c1b" → end of acc-1's calendar list.
        sidebar._drop_insert_idx = 2
        _drop(sidebar, mime, QPoint(50, 200))
        qapp.processEvents()

        assert received == ["acc-1"], (
            f"calendar_order_changed should fire once with 'acc-1', got {received}"
        )
        assert captured["calendar"] is not None
        order = [cid for cid, _ in captured["calendar"]]
        assert order == ["c1b", "c1a"], (
            f"unexpected calendar order after drop: {order}"
        )
    finally:
        sidebar._stop_auto_scroll()
        sidebar.close()
        sidebar.deleteLater()


def test_account_drop_at_same_position_emits_no_signal(qapp) -> None:
    """Dropping an account at its current position is a no-op (no signal)."""
    sidebar, captured = _build_sidebar(qapp)
    try:
        received: list = []
        sidebar.account_order_changed.connect(lambda: received.append(True))

        mime = _account_mime("acc-1")
        _drag_enter(sidebar, mime)
        # acc-1 is index 0; "insert at index 0" → no-op.
        sidebar._drop_insert_idx = 0
        _drop(sidebar, mime, QPoint(50, 10))
        qapp.processEvents()

        assert received == [], (
            "no-op drop should not emit account_order_changed"
        )
        assert captured["account"] is None
    finally:
        sidebar._stop_auto_scroll()
        sidebar.close()
        sidebar.deleteLater()
