"""Centralised palette and typography tokens for the UI layer.

Single source of truth for hex colors and font sizes used by the graphics-view
calendar widgets. The QSS files (styles/dark.qss, styles/light.qss) style the
generic Qt widgets and currently use a near-identical palette - keep them in
sync with the constants here when tweaking.
"""

from __future__ import annotations

# ── Dark palette (default) ───────────────────────────────────────────────
# Surface colours line up with the actively-used dark.qss values so the
# graphics-view canvases blend with the surrounding Qt chrome.
BG_BASE = "#0e0e0e"  # window background
BG_SURFACE = "#1a1a1a"  # toolbars, status, headers
BG_SURFACE_ALT = "#262626"  # inputs, hover, today tint base
BG_SURFACE_3 = "#333333"  # selected/focused fill, agenda day header
BG_WEEKEND = "#141414"  # weekend column tint (very subtle)
BG_TIME_AXIS = "#161616"  # time-axis column

BORDER = "#3a3a3a"  # subtle grid lines
BORDER_STRONG = "#7a7a7a"  # view dividers, header bottom

TEXT_PRIMARY = "#ffffff"  # event titles, day numbers
TEXT_SECONDARY = "#c8c8c8"  # secondary text, time-axis labels
TEXT_DISABLED = "#8a8a8a"  # spillover days, placeholders

ACCENT = "#7eb5ff"  # ring around today, focus
ACCENT_FILL = "#3b82f6"  # filled-accent surfaces (selected buttons)
DANGER = "#ff6b6b"  # now-line, errors
SUCCESS = "#6ee896"  # sync ok

# Event chip defaults (used when no event/calendar color is set)
CHIP_FALLBACK = "#9ec5ff"

# ── Typography (point sizes) ─────────────────────────────────────────────
FONT_FAMILY = "sans-serif"
FONT_BASE = 10  # body, default
FONT_DAY_NUMBER = 13  # month day numbers, week day headers
FONT_DAY_HEADER = 12  # day-view header (long date)
FONT_TIME_AXIS = 9  # hour labels
FONT_CHIP_TITLE = 8  # event chip title
FONT_CHIP_PREFIX = 7  # event chip time prefix
FONT_CHIP_LOCATION = 7  # event chip location line
FONT_MONTH_HEADER = 9  # day-of-week strip above month grid

# ── Chip layout thresholds (pixels) ──────────────────────────────────────
# Spec §4.2: visibility tiers for event chips.
CHIP_MIN_TITLE_H = 14        # below this, chip is solid color (tooltip only)
CHIP_MIN_INLINE_TIME_H = 18  # below this (≥14): title only; at/above: time+title inline
CHIP_MIN_PREFIX_H = 24       # at/above: time on its own row above title
CHIP_MIN_LOCATION_H = 38     # below this, location is suppressed
CHIP_MIN_LOCATION_MULTILINE_H = 50  # at/above: location wraps to up to 2 lines

# ── Multi-day continuation glyphs ────────────────────────────────────────
GLYPH_CONTINUES_RIGHT = "→"
GLYPH_CONTINUES_LEFT = "←"
