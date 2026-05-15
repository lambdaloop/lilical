"""Tests for _recurrence_actions.py dispatch logic."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


class _FakeStore:
    def __init__(self):
        self.update_calls = []
        self.update_instance_calls = []
        self.split_series_calls = []
        self.truncate_calls = []
        self.delete_calls = []
        self.delete_instance_calls = []

    def queue_update(self, event, etag):
        self.update_calls.append((event, etag))

    def queue_update_instance(self, uid, calendar_id, recurrence_id_dt, edited):
        self.update_instance_calls.append((uid, calendar_id, recurrence_id_dt, edited))

    def queue_split_series(self, uid, calendar_id, split_at_dt, edited_event_for_tail):
        self.split_series_calls.append(
            (uid, calendar_id, split_at_dt, edited_event_for_tail)
        )
        return "new-uid"

    def queue_truncate_series(self, uid, calendar_id, until_dt):
        self.truncate_calls.append((uid, calendar_id, until_dt))

    def queue_delete(self, uid, calendar_id):
        self.delete_calls.append((uid, calendar_id))

    def queue_delete_instance(self, uid, calendar_id, recurrence_id_dt):
        self.delete_instance_calls.append((uid, calendar_id, recurrence_id_dt))

    def get_event(self, uid, calendar_id):
        return None


def _make_event(**kwargs):
    from lilical.models.event import Event

    defaults = dict(
        uid="uid-series",
        calendar_id="cal-1",
        summary="Recurring Meeting",
        rrule="FREQ=WEEKLY",
    )
    defaults.update(kwargs)
    return Event(**defaults)


def test_dispatch_edit_occurrence(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = _FakeStore()
    parent = QWidget()
    instance_dt = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    event = _make_event()
    edited = _make_event(summary="Edited")

    _dispatch_edit(parent, store, event, instance_dt, edited, "occurrence")

    assert len(store.update_instance_calls) == 1
    uid, cal_id, rid, ed = store.update_instance_calls[0]
    assert uid == "uid-series"
    assert rid == instance_dt

    parent.deleteLater()


def test_dispatch_edit_following_calls_split_series(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = _FakeStore()
    parent = QWidget()
    instance_dt = datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc)
    event = _make_event()
    edited = _make_event(summary="Edited tail")

    _dispatch_edit(parent, store, event, instance_dt, edited, "following")

    assert len(store.split_series_calls) == 1
    uid, cal_id, split_at, tail = store.split_series_calls[0]
    assert uid == "uid-series"
    assert split_at == instance_dt

    parent.deleteLater()


def test_dispatch_edit_series_calls_queue_update(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = _FakeStore()
    parent = QWidget()
    event = _make_event()
    edited = _make_event(summary="Series Edit")

    _dispatch_edit(parent, store, event, None, edited, "series")

    assert len(store.update_calls) == 1

    parent.deleteLater()


def test_dispatch_delete_occurrence_calls_delete_instance(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = _FakeStore()
    parent = QWidget()
    instance_dt = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    event = _make_event()

    _dispatch_delete(parent, store, event, instance_dt, "occurrence")

    assert len(store.delete_instance_calls) == 1
    uid, cal_id, rid = store.delete_instance_calls[0]
    assert uid == "uid-series"
    assert rid == instance_dt

    parent.deleteLater()


def test_dispatch_delete_following_calls_truncate(qapp):
    from PySide6.QtWidgets import QWidget

    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = _FakeStore()
    parent = QWidget()
    instance_dt = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    event = _make_event()

    _dispatch_delete(parent, store, event, instance_dt, "following")

    assert len(store.truncate_calls) == 1
    uid, cal_id, until_dt = store.truncate_calls[0]
    assert uid == "uid-series"
    assert until_dt == instance_dt

    parent.deleteLater()


def test_dispatch_edit_occurrence_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget
    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = _FakeStore()
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Edited via rid")

    _dispatch_edit(parent, store, event, None, edited, "occurrence")

    assert len(store.update_instance_calls) == 1
    uid, cal_id, rid_call, ed = store.update_instance_calls[0]
    assert rid_call == rid
    parent.deleteLater()


def test_dispatch_edit_following_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget
    from lilical.ui.views._recurrence_actions import _dispatch_edit

    store = _FakeStore()
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Split via rid")

    _dispatch_edit(parent, store, event, None, edited, "following")

    assert len(store.split_series_calls) == 1
    parent.deleteLater()


def test_dispatch_edit_series_with_recurrence_id_looks_up_master(qapp):
    from PySide6.QtWidgets import QWidget
    from lilical.ui.views._recurrence_actions import _dispatch_edit

    class _MasterLookupStore(_FakeStore):
        def get_event(self, uid, calendar_id):
            return _make_event(etag='"master-etag"')

    store = _MasterLookupStore()
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)
    edited = _make_event(summary="Series edit via rid")

    _dispatch_edit(parent, store, event, None, edited, "series")

    assert len(store.update_calls) == 1
    parent.deleteLater()


def test_dispatch_delete_occurrence_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget
    from lilical.ui.views._recurrence_actions import _dispatch_delete

    store = _FakeStore()
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)

    _dispatch_delete(parent, store, event, None, "occurrence")

    assert len(store.delete_instance_calls) == 1
    parent.deleteLater()


def test_dispatch_delete_following_with_recurrence_id(qapp):
    from PySide6.QtWidgets import QWidget
    from lilical.ui.views._recurrence_actions import _dispatch_delete

    class _MasterLookupStore(_FakeStore):
        def get_event(self, uid, calendar_id):
            return _make_event()

    store = _MasterLookupStore()
    parent = QWidget()
    rid = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    event = _make_event(recurrence_id=rid)

    _dispatch_delete(parent, store, event, None, "following")

    assert len(store.truncate_calls) == 1
    parent.deleteLater()
