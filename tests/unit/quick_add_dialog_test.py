"""Tests that QuickAddDialog's calendar picker excludes read-only calendars."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from lilical.storage.event_store import EventStore

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


class _StoreWithCalendars:
    def __init__(self, calendars: list[tuple[str, str, str]]) -> None:
        self._account = SimpleNamespace(id="acc-1", display_name="Acc")
        self._cals = [
            SimpleNamespace(
                id=cid,
                account_id="acc-1",
                provider_id=cid,
                display_name=name,
                color="#5e9fff",
                access_role=role,
                is_included=1,
                is_visible=1,
            )
            for (cid, name, role) in calendars
        ]

    def list_accounts(self, enabled_only=True):
        return [self._account]

    def list_calendars(self, account_id, included_only=True):
        return [c for c in self._cals if c.account_id == account_id]


def _picker_ids(dialog) -> list:
    return [
        dialog._cal_combo.itemData(i) for i in range(dialog._cal_combo.count())
    ]


def test_picker_excludes_reader_and_freebusyreader(qapp):
    from lilical.ui.widgets.quick_add_dialog import QuickAddDialog

    store = _StoreWithCalendars(
        [
            ("cal-work", "Work", "owner"),
            ("cal-sub", "Subscription", "reader"),
            ("cal-fb", "FB peek", "freebusyreader"),
            ("cal-shared", "Shared write", "writer"),
        ]
    )
    dialog = QuickAddDialog(store=cast(EventStore, store))
    ids = _picker_ids(dialog)
    assert "cal-work" in ids
    assert "cal-shared" in ids
    assert "cal-sub" not in ids
    assert "cal-fb" not in ids
    dialog.deleteLater()


def test_picker_empty_when_only_read_only_calendars(qapp):
    from lilical.ui.widgets.quick_add_dialog import QuickAddDialog

    store = _StoreWithCalendars(
        [
            ("cal-sub", "Subscription", "reader"),
            ("cal-fb", "FB", "freebusyreader"),
        ]
    )
    dialog = QuickAddDialog(store=cast(EventStore, store))
    assert dialog._cal_combo.count() == 0
    dialog.deleteLater()
