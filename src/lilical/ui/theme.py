"""Centralised palette and typography tokens for the UI layer.

Single source of truth for colors and font sizes used by the graphics-view
calendar widgets. Call `apply("light")` or `apply("dark")` to switch palette;
all paint code reads the module-level names at paint time so a viewport
repaint is all that is needed afterward.
"""

from __future__ import annotations

# ── Palettes ─────────────────────────────────────────────────────────────

_DARK: dict[str, str] = {
    "BG_BASE": "#0e0e0e",  # window background
    "BG_SURFACE": "#1a1a1a",  # toolbars, status, headers
    "BG_SURFACE_ALT": "#262626",  # inputs, hover, today tint base
    "BG_SURFACE_3": "#333333",  # selected/focused fill, agenda day header
    "BG_WEEKEND": "#141414",  # weekend column tint (very subtle)
    "BG_TIME_AXIS": "#161616",  # time-axis column
    "BORDER": "#3a3a3a",  # subtle grid lines
    "BORDER_STRONG": "#7a7a7a",  # view dividers, header bottom
    "TEXT_PRIMARY": "#ffffff",  # event titles, day numbers
    "TEXT_SECONDARY": "#c8c8c8",  # secondary text, time-axis labels
    "TEXT_DISABLED": "#8a8a8a",  # spillover days, placeholders
    "ACCENT": "#7eb5ff",  # ring around today, focus
    "ACCENT_FILL": "#3b82f6",  # filled-accent surfaces (selected buttons)
    "DANGER": "#ff6b6b",  # now-line, errors
    "SUCCESS": "#6ee896",  # sync ok
    "CHIP_FALLBACK": "#9ec5ff",  # event chip default color
}

_LIGHT: dict[str, str] = {
    "BG_BASE": "#ffffff",
    "BG_SURFACE": "#f0f0f0",
    "BG_SURFACE_ALT": "#e4e4e4",
    "BG_SURFACE_3": "#d8d8d8",
    "BG_WEEKEND": "#f8f8f8",
    "BG_TIME_AXIS": "#f0f0f0",
    "BORDER": "#d4d4d4",
    "BORDER_STRONG": "#a0a0a0",
    "TEXT_PRIMARY": "#111111",
    "TEXT_SECONDARY": "#555555",
    "TEXT_DISABLED": "#a0a0a0",
    "ACCENT": "#2563eb",
    "ACCENT_FILL": "#2563eb",
    "DANGER": "#dc2626",
    "SUCCESS": "#16a34a",
    "CHIP_FALLBACK": "#3b82f6",
}


def apply(name: str) -> None:
    """Switch the active palette. Call before triggering a view repaint."""
    palette = _LIGHT if name == "light" else _DARK
    g = globals()
    for key, value in palette.items():
        g[key] = value


# ── Scale ─────────────────────────────────────────────────────────────────

UI_SCALE_PRESETS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.1, 1.25, 1.5, 2.0, 3.0)

UI_SCALE: float = 1.0

_BASE_FONT_BASE = 11
_BASE_FONT_DAY_NUMBER = 14
_BASE_FONT_DAY_HEADER = 14
_BASE_FONT_TIME_AXIS = 10
_BASE_FONT_CHIP_TITLE = 9
_BASE_FONT_CHIP_PREFIX = 8
_BASE_FONT_CHIP_LOCATION = 8
_BASE_FONT_MONTH_HEADER = 10


def apply_scale(factor: float) -> None:
    """Scale typography constants. Call before a view repaint."""
    g = globals()
    g["UI_SCALE"] = factor
    g["FONT_BASE"] = max(1, round(_BASE_FONT_BASE * factor))
    g["FONT_DAY_NUMBER"] = max(1, round(_BASE_FONT_DAY_NUMBER * factor))
    g["FONT_DAY_HEADER"] = max(1, round(_BASE_FONT_DAY_HEADER * factor))
    g["FONT_TIME_AXIS"] = max(1, round(_BASE_FONT_TIME_AXIS * factor))
    g["FONT_CHIP_TITLE"] = max(1, round(_BASE_FONT_CHIP_TITLE * factor))
    g["FONT_CHIP_PREFIX"] = max(1, round(_BASE_FONT_CHIP_PREFIX * factor))
    g["FONT_CHIP_LOCATION"] = max(1, round(_BASE_FONT_CHIP_LOCATION * factor))
    g["FONT_MONTH_HEADER"] = max(1, round(_BASE_FONT_MONTH_HEADER * factor))


def ui_base_font_pt() -> int:
    return max(1, round(_BASE_FONT_BASE * UI_SCALE))


def apply_all_scales(factor: float) -> None:
    """Scale theme constants and all view/widget layout constants."""
    apply_scale(factor)
    from lilical.ui.views import day, month, week  # noqa: PLC0415
    from lilical.ui.widgets import mini_month  # noqa: PLC0415

    month.apply_scale(factor)
    week.apply_scale(factor)
    day.apply_scale(factor)
    mini_month.apply_scale(factor)


# Initialise with dark palette (same values as _DARK above).
BG_BASE = _DARK["BG_BASE"]
BG_SURFACE = _DARK["BG_SURFACE"]
BG_SURFACE_ALT = _DARK["BG_SURFACE_ALT"]
BG_SURFACE_3 = _DARK["BG_SURFACE_3"]
BG_WEEKEND = _DARK["BG_WEEKEND"]
BG_TIME_AXIS = _DARK["BG_TIME_AXIS"]

BORDER = _DARK["BORDER"]
BORDER_STRONG = _DARK["BORDER_STRONG"]

TEXT_PRIMARY = _DARK["TEXT_PRIMARY"]
TEXT_SECONDARY = _DARK["TEXT_SECONDARY"]
TEXT_DISABLED = _DARK["TEXT_DISABLED"]

ACCENT = _DARK["ACCENT"]
ACCENT_FILL = _DARK["ACCENT_FILL"]
DANGER = _DARK["DANGER"]
SUCCESS = _DARK["SUCCESS"]

CHIP_FALLBACK = _DARK["CHIP_FALLBACK"]

# ── Typography (point sizes) ─────────────────────────────────────────────
FONT_FAMILY = "sans-serif"
FONT_BASE = 11  # body, default
FONT_DAY_NUMBER = 14  # month day numbers, week day headers
FONT_DAY_HEADER = 14  # day-view header (long date)
FONT_TIME_AXIS = 10  # hour labels
FONT_CHIP_TITLE = 9  # event chip title
FONT_CHIP_PREFIX = 8  # event chip time prefix
FONT_CHIP_LOCATION = 8  # event chip location line
FONT_MONTH_HEADER = 10  # day-of-week strip above month grid

# ── Multi-day continuation glyphs ────────────────────────────────────────
GLYPH_CONTINUES_RIGHT = "→"
GLYPH_CONTINUES_LEFT = "←"

# ── Dense-overlap cluster rendering ──────────────────────────────────────
CLUSTER_LINE_WIDTH_PX = 4
CLUSTER_LINE_GAP_PX = 2          # 6 px center-to-center
CLUSTER_SPINE_MAX_FRAC = 0.4     # cap spine as fraction of column width
