"""Tests for pure helpers and UI wiring in ConflictDialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from lilical.ui.widgets.conflict_dialog import _fmt


# ── _fmt (pure, no Qt needed) ─────────────────────────────────────────────────


def test_fmt_none_returns_dash() -> None:
    assert _fmt(None) == "—"


def test_fmt_empty_string_returns_dash() -> None:
    assert _fmt("") == "—"


def test_fmt_empty_tuple_returns_dash() -> None:
    assert _fmt(()) == "—"


def test_fmt_empty_list_returns_dash() -> None:
    assert _fmt([]) == "—"


def test_fmt_string_returns_itself() -> None:
    assert _fmt("confirmed") == "confirmed"


def test_fmt_list_of_strings_joins_with_comma() -> None:
    assert _fmt(["a", "b", "c"]) == "a, b, c"


def test_fmt_tuple_of_strings_joins_with_comma() -> None:
    assert _fmt(("x", "y")) == "x, y"


def test_fmt_single_element_list() -> None:
    assert _fmt(["only"]) == "only"


def test_fmt_datetime_returns_str_representation() -> None:
    from datetime import datetime, timezone

    dt = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    result = _fmt(dt)
    assert "2026" in result
    assert "09" in result or "9" in result


def test_fmt_integer() -> None:
    assert _fmt(42) == "42"


# ── ConflictDialog.choice (needs Qt offscreen) ────────────────────────────────


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_choice_defaults_to_local(qapp) -> None:
    from lilical.models.event import Event
    from lilical.ui.widgets.conflict_dialog import ConflictDialog

    ev = Event(uid="u1", calendar_id="c1", summary="Test")
    dlg = ConflictDialog(local=ev, remote=ev)
    assert dlg.choice == "local"


def test_choice_reflects_remote_selection(qapp) -> None:
    from lilical.models.event import Event
    from lilical.ui.widgets.conflict_dialog import ConflictDialog

    ev = Event(uid="u1", calendar_id="c1")
    dlg = ConflictDialog(local=ev, remote=ev)
    dlg._remote_radio.setChecked(True)
    assert dlg.choice == "remote"


def test_choice_reflects_merge_selection(qapp) -> None:
    from lilical.models.event import Event
    from lilical.ui.widgets.conflict_dialog import ConflictDialog

    ev = Event(uid="u1", calendar_id="c1")
    dlg = ConflictDialog(local=ev, remote=ev)
    dlg._merge_radio.setChecked(True)
    assert dlg.choice == "merge"
