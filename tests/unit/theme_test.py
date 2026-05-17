"""Tests for ui.theme palette switching."""

from __future__ import annotations

import lilical.ui.theme as theme


def test_apply_light_switches_color_tokens() -> None:
    """apply('light') switches globals to _LIGHT values."""
    dark_bg = theme.BG_BASE
    dark_text = theme.TEXT_PRIMARY

    theme.apply("light")
    assert theme.BG_BASE == "#ffffff"
    assert theme.TEXT_PRIMARY == "#111111"
    assert theme.BORDER == "#d4d4d4"
    assert theme.ACCENT == "#2563eb"

    theme.apply("dark")
    assert dark_bg == theme.BG_BASE
    assert dark_text == theme.TEXT_PRIMARY


def test_apply_unknown_name_uses_dark() -> None:
    """An unrecognized palette name defaults to _DARK."""
    theme.apply("unknown")
    assert theme.BG_BASE == "#0e0e0e"
