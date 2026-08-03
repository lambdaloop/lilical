"""AgendaView must actually re-render when the display zone changes.

`_query_agenda_data` short-circuits when the instance snapshot is unchanged —
and a zone change leaves the instance set byte-identical, so without clearing
the snapshot the refresh is swallowed and stale times stay on screen.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


def _agenda(qapp):
    from lilical.ui.views.agenda import AgendaView

    from .conftest import make_fake_store

    return AgendaView(make_fake_store(), cal_info_provider=lambda: {})


def test_set_display_tz_clears_snapshot(qapp) -> None:
    view = _agenda(qapp)
    try:
        view._snapshot = frozenset({("uid", "2026-05-13T09:00:00+00:00", "cal")})
        view._snapshot_start = date(2026, 5, 13)

        view.set_display_tz("Asia/Tokyo")

        assert view._snapshot == frozenset()
        assert view._snapshot_start is None
    finally:
        view.close()
        view.deleteLater()


def test_query_short_circuit_would_swallow_a_zone_change(qapp) -> None:
    """Documents *why* the snapshot has to be cleared.

    With the snapshot intact and the same instances, the off-thread query
    returns None — i.e. "nothing to redraw" — even though every rendered time
    just moved.
    """
    from lilical.ui.views.agenda import _query_agenda_data

    inst = SimpleNamespace(
        uid="uid",
        calendar_id="cal",
        dtstart_local="2026-05-13T09:00:00+00:00",
        dtend_local="2026-05-13T10:00:00+00:00",
        dtstart_utc=int(datetime(2026, 5, 13, 9, tzinfo=timezone.utc).timestamp()),
        all_day=0,
    )

    class _Store:
        def list_instances(self, _s, _e, calendar_ids=None):
            return [inst]

        def events_for_instances(self, _i):
            return {}

        def completion_for_instances(self, _i):
            return frozenset()

    snapshot = frozenset({(inst.uid, inst.dtstart_local, inst.calendar_id)})
    start = date(2026, 5, 13)

    assert (
        _query_agenda_data(_Store(), start, start, {}, snapshot, start) is None
    ), "expected the short-circuit to fire"
    # Cleared snapshot → the query proceeds and the view redraws.
    assert _query_agenda_data(_Store(), start, start, {}, frozenset(), None) is not None
