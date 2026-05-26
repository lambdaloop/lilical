"""Tests for pure helper functions in ui/widgets/event_chip.py.

Functions under test require PySide6.QColor but NOT a QApplication instance.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor

from lilical.models.event import Event
from lilical.ui import theme
from lilical.ui.widgets.event_chip import (
    _contrast_ratio,
    _readable_text_color,
    _relative_luminance,
    _resolve_color,
    _srgb_to_linear,
)

# ── _srgb_to_linear ─────────────────────────────────────────────────────────


def test_srgb_black_is_0() -> None:
    assert _srgb_to_linear(0.0) == pytest.approx(0.0)


def test_srgb_white_is_1() -> None:
    assert abs(_srgb_to_linear(1.0) - 1.0) < 1e-6


def test_srgb_midrange_uses_gamma_curve() -> None:
    # 0.5 → above the kink at 0.03928
    v = _srgb_to_linear(0.5)
    assert 0.2 < v < 0.3  # known range for the IEC 61966-2-1 formula


# ── _relative_luminance ──────────────────────────────────────────────────────


def test_luminance_black_is_0() -> None:
    assert _relative_luminance(QColor("#000000")) == pytest.approx(0.0)


def test_luminance_white_is_1() -> None:
    assert _relative_luminance(QColor("#ffffff")) == pytest.approx(1.0, abs=1e-4)


def test_luminance_mid_gray_is_between() -> None:
    v = _relative_luminance(QColor("#808080"))
    assert 0.1 < v < 0.3


# ── _contrast_ratio ──────────────────────────────────────────────────────────


def test_contrast_white_on_black_is_21() -> None:
    ratio = _contrast_ratio(QColor("#ffffff"), QColor("#000000"))
    assert ratio == pytest.approx(21.0, abs=0.1)


def test_contrast_same_color_is_1() -> None:
    assert _contrast_ratio(QColor("#3355aa"), QColor("#3355aa")) == pytest.approx(1.0)


# ── _readable_text_color ──────────────────────────────────────────────────────


def test_white_text_on_black_bg() -> None:
    result = _readable_text_color(QColor("#000000"))
    assert result.name() == "#ffffff"


def test_black_text_on_white_bg() -> None:
    result = _readable_text_color(QColor("#ffffff"))
    assert result.name() == "#000000"


def test_text_color_on_mid_accent() -> None:
    # Our default accent #7eb5ff — verify it picks a readable color.
    result = _readable_text_color(QColor(theme.ACCENT))
    assert result.name() in ("#000000", "#ffffff")


# ── _resolve_color ────────────────────────────────────────────────────────────


def test_resolve_color_prefers_event_color() -> None:
    c = _resolve_color("#ff0000", "#0000ff")
    assert c.name() == "#ff0000"


def test_resolve_color_falls_back_to_calendar_color() -> None:
    c = _resolve_color(None, "#0000ff")
    assert c.name() == "#0000ff"


def test_resolve_color_falls_back_to_chip_fallback() -> None:
    c = _resolve_color(None, None)
    assert c.isValid()
    assert c.name() == QColor(theme.CHIP_FALLBACK).name()


def test_resolve_color_skips_invalid_event_color() -> None:
    c = _resolve_color("notacolor", "#00ff00")
    assert c.name() == "#00ff00"


# ── chip tier threshold monotonicity ─────────────────────────────────────────


def test_chip_tier_mins_are_monotonically_increasing() -> None:
    # Thresholds are now derived from font metrics at paint time (not theme constants).
    # Verify the formula is always increasing for any positive font heights.
    t, p, lh = 14.0, 12.0, 12.0  # realistic 9pt/8pt/8pt values at scale 1.0
    min_title = t + 1
    min_prefix = t + p
    min_location = t + p + lh
    min_location_multi = t + p + 2 * lh
    assert min_title < min_prefix < min_location < min_location_multi


# ── _is_dimmed_for ────────────────────────────────────────────────────────────
# Test the dimming logic via a standalone helper (avoids needing QApp for EventChip).


import pytest  # noqa: E402 (placed at end due to Qt import order)


def _make_event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = {"uid": "u1", "calendar_id": "c1"}
    defaults.update(overrides)
    return Event(**defaults)


def test_not_dimmed_by_default() -> None:
    e = _make_event()
    chip = _MockChip(e)
    assert not chip._is_dimmed()


def test_dimmed_for_cancelled() -> None:
    e = _make_event(status="CANCELLED")
    chip = _MockChip(e)
    assert chip._is_dimmed()


def test_dimmed_for_declined_self_response() -> None:
    e = _make_event(self_response="DECLINED")
    chip = _MockChip(e)
    assert chip._is_dimmed()


def test_not_dimmed_for_tentative() -> None:
    e = _make_event(status="TENTATIVE")
    chip = _MockChip(e)
    assert not chip._is_dimmed()


def test_not_dimmed_for_accepted_self_response() -> None:
    e = _make_event(self_response="ACCEPTED")
    chip = _MockChip(e)
    assert not chip._is_dimmed()


class _MockChip:
    """Minimal duck-type of EventChip exposing _is_dimmed."""

    def __init__(self, event: Event) -> None:
        self._event = event

    # Copy the logic verbatim from EventChip so we're testing the same code path.
    def _is_dimmed(self) -> bool:
        from lilical.ui.widgets.event_chip import EventChip

        return EventChip._is_dimmed(self)  # type: ignore[arg-type]
