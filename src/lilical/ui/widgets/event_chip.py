from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QGraphicsObject, QGraphicsSceneContextMenuEvent, QMenu

from lilical.ui import theme
from lilical.utils.timezone import local_zoneinfo

if TYPE_CHECKING:
    from lilical.models.event import Event


class ChipMode(Enum):
    BARS = "bars"  # solid fill, title overlaid in contrasting text
    TEXT = "text"  # 3 px coloured left bar, neutral background
    DOT = "dot"  # tiny coloured dot + title, for ultra-tight rows


def _resolve_color(event_color: str | None, fallback: str | None) -> QColor:
    """Pick the first valid colour from (event-own, calendar-fallback, default)."""
    for candidate in (event_color, fallback, theme.CHIP_FALLBACK):
        if not candidate:
            continue
        c = QColor(candidate)
        if c.isValid():
            return c
    return QColor(theme.CHIP_FALLBACK)


def _srgb_to_linear(channel: float) -> float:
    """sRGB → linear-light per WCAG 2.x."""
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(c: QColor) -> float:
    r = _srgb_to_linear(c.redF())
    g = _srgb_to_linear(c.greenF())
    b = _srgb_to_linear(c.blueF())
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: QColor, b: QColor) -> float:
    la = _relative_luminance(a)
    lb = _relative_luminance(b)
    lighter, darker = (la, lb) if la > lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


def _readable_text_color(bg: QColor) -> QColor:
    """Return whichever of pure white/black yields the higher contrast against
    `bg`. Always >= 4.5:1 for the colours typical in BC2-style palettes
    (saturated mid-tones); for chosen "designer" colours close to luminance
    threshold it still picks the better of the two extremes."""
    white = QColor("#ffffff")
    black = QColor("#000000")
    if _contrast_ratio(white, bg) >= _contrast_ratio(black, bg):
        return white
    return black


def _desaturate(c: QColor, factor: float) -> QColor:
    """Reduce saturation of `c` to `factor` fraction (0 = gray, 1 = unchanged)."""
    h, s, lum, a = c.getHslF()
    result = QColor()
    result.setHslF(h, max(0.0, s * factor), lum, a)
    return result


def _ellipsize(painter: QPainter, text: str, max_w: float) -> str:
    """Single-line ellipsize using the painter's current font."""
    fm = QFontMetricsF(painter.font())
    return fm.elidedText(text, Qt.TextElideMode.ElideRight, max_w)


def _draw_tight_wrapped(
    painter: QPainter, text: str, font: QFont, rect: QRectF
) -> None:
    """Word-wrap `text` into `rect` with no font leading between lines.

    Uses ascent+descent as the line step instead of height() (which adds
    leading, typically 1-2 px at 9 pt). Caller must set a clip rect; lines
    that extend past rect.height() are silently discarded.
    """
    fm = QFontMetricsF(font)
    line_step = fm.ascent() + fm.descent()
    max_h = rect.height()

    layout = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    layout.setTextOption(opt)

    layout.beginLayout()
    y = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        y += line_step
        if y >= max_h:
            # Drain remaining text off-screen so endLayout() closes cleanly.
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(rect.width())
                line.setPosition(QPointF(0, max_h + line_step))
            break
    layout.endLayout()
    layout.draw(painter, rect.topLeft())


def _coerce_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _format_time_prefix_from(event: "Event", time_format: str = "24h") -> str | None:
    """Fallback time-prefix derived from the master Event (not instance-aware)."""
    if event.all_day:
        return None
    start = _coerce_dt(event.dtstart)
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=local_zoneinfo())
    fmt = "%-I:%M %p" if time_format == "12h" else "%H:%M"
    return start.astimezone().strftime(fmt)


# Width below which the overlap badge is shown.
_BADGE_CHIP_MAX_W = 44


class EventChip(QGraphicsObject):
    """Colored rectangle representing one calendar event in a graphics view.

    Rendering tiers (height-based, per UI-SPEC §1.5 + §4.2):
        h <  14 px : solid fill, no text (tooltip carries title)
        h >= 14 px : title only, vertically centred
        h >= 18 px : time prefix + title on same row, vertically centred
        h >= 24 px : time prefix on its own row, then title (wraps)
        h >= 38 px : + location (if event.location set)
        h >= 52 px : location wraps to up to 2 lines
    """

    details_requested = Signal(object)  # emits Event — left-click opens read-only view
    edit_requested = Signal(object)  # emits Event — right-click → Edit
    delete_requested = Signal(object)  # emits Event
    # Drag signals: see docstring for the chip-drag state machine.
    # Payload: (event, mode, scene_pos). mode ∈ {"move", "resize_top",
    # "resize_bottom"}.
    drag_progress = Signal(object, str, "QPointF")  # type: ignore[reportArgumentType]
    drag_committed = Signal(object, str, "QPointF")  # type: ignore[reportArgumentType]
    drag_cancelled = Signal(object)

    # Drag zone constants (pixels in local/scene coords — chips have an
    # identity transform so the two are equivalent here).
    EDGE_RESIZE_PX = 6
    MOVE_THRESHOLD_PX = 4
    # Below this chip height, treat the whole rect as body (edge zones would
    # otherwise consume nearly all of it).
    MIN_HEIGHT_FOR_EDGE_RESIZE = 18

    def __init__(
        self,
        event: "Event",
        rect: QRectF,
        *,
        calendar_color: str | None = None,
        mode: ChipMode = ChipMode.BARS,
        show_time_prefix: bool = True,
        time_prefix: str | None = None,
        time_format: str = "24h",
        continues_left: bool = False,
        continues_right: bool = False,
        overlap_cols: int = 1,
        instance_dtstart: datetime | None = None,
        completed: bool = False,
    ) -> None:
        super().__init__()
        self._event = event
        self._rect = rect
        self._calendar_color = calendar_color
        self._mode = mode
        self._show_time_prefix = show_time_prefix
        self._time_prefix = time_prefix
        self._time_format = time_format
        self._continues_left = continues_left
        self._continues_right = continues_right
        self._overlap_cols = overlap_cols
        self._hovered = False
        self._instance_dtstart = instance_dtstart
        self._completed: bool = completed
        self._completed_display_enabled: bool = False
        # Drag state
        self._drag_mode: str | None = None
        self._press_scene_pos: QPointF | None = None
        self.setAcceptHoverEvents(True)
        self.setToolTip(self._build_tooltip())

    def _prefix_text(self) -> str | None:
        if not self._show_time_prefix:
            return None
        if self._time_prefix is not None:
            return self._time_prefix or None
        return _format_time_prefix_from(self._event, self._time_format)

    def _build_tooltip(self) -> str:
        parts = [self._event.summary or "(no title)"]
        prefix = self._prefix_text()
        if prefix:
            parts.insert(0, prefix)
        if self._event.location:
            parts.append(self._event.location)
        # Surface cancellation/decline in the tooltip too — the visual dim
        # plus strikethrough is easy to miss on dense calendars.
        if self._is_dimmed():
            reason = self._dim_reason()
            if reason:
                parts.append(f"({reason})")
        elif self._is_needs_action():
            sr = (self._event.self_response or "").upper()
            parts.append("(tentative)" if sr == "TENTATIVE" else "(awaiting response)")
        if self._is_completed():
            parts.append("(completed)")
        return "\n".join(parts)

    def _is_completed(self) -> bool:
        return self._completed and self._completed_display_enabled

    def set_completed_display(self, enabled: bool) -> None:
        if enabled == self._completed_display_enabled:
            return
        self._completed_display_enabled = enabled
        self.setToolTip(self._build_tooltip())
        self.update()

    def _is_dimmed(self) -> bool:
        """An event is dimmed when it's cancelled or the user declined."""
        status = (self._event.status or "").upper()
        if status == "CANCELLED":
            return True
        sr = (self._event.self_response or "").upper()
        return sr == "DECLINED"

    def _is_needs_action(self) -> bool:
        """True when the user hasn't committed (no response or tentative)."""
        sr = (self._event.self_response or "").upper()
        return sr in ("NEEDS-ACTION", "TENTATIVE")

    def _dim_reason(self) -> str | None:
        status = (self._event.status or "").upper()
        if status == "CANCELLED":
            return "cancelled"
        sr = (self._event.self_response or "").upper()
        if sr == "DECLINED":
            return "declined"
        return None

    @property
    def instance_dtstart(self) -> datetime | None:
        """The specific occurrence's start time, used for 'edit this occurrence'."""
        return self._instance_dtstart

    def update_layout(
        self,
        rect: QRectF,
        *,
        calendar_color: str | None,
        time_prefix: str | None,
        show_time_prefix: bool,
        continues_left: bool = False,
        continues_right: bool = False,
        overlap_cols: int = 1,
        instance_dtstart: datetime | None,
    ) -> None:
        """Update geometry and layout properties in place — avoids chip reallocation."""
        if rect != self._rect:
            self.prepareGeometryChange()
            self._rect = rect
        self._calendar_color = calendar_color
        self._time_prefix = time_prefix
        self._show_time_prefix = show_time_prefix
        self._continues_left = continues_left
        self._continues_right = continues_right
        self._overlap_cols = overlap_cols
        self._instance_dtstart = instance_dtstart
        self.setToolTip(self._build_tooltip())
        self.update()

    def update_event_data(self, event: "Event", *, completed: bool = False) -> None:
        """Refresh event data without changing geometry; skipped if chip is mid-drag."""
        if self._drag_mode is not None:
            return
        self._event = event
        self._completed = completed
        self.setToolTip(self._build_tooltip())
        self.update()

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        base = _resolve_color(self._event.color, self._calendar_color)
        if self._is_completed():
            base = _desaturate(base, 0.30)
        if self._hovered:
            base = base.lighter(115)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Cancelled/declined events are drawn at reduced opacity; completed
        # events are drawn at 0.60 opacity (dimmed wins when both apply).
        _needs_restore = False
        if self._is_dimmed():
            painter.save()
            painter.setOpacity(0.45)
            _needs_restore = True
        elif self._is_completed():
            painter.save()
            painter.setOpacity(0.60)
            _needs_restore = True

        if self._mode is ChipMode.DOT:
            self._paint_dot(painter, base)
        elif self._mode is ChipMode.TEXT:
            self._paint_text_mode(painter, base)
        else:
            # BARS mode (default)
            self._paint_bars_mode(painter, base)

        if _needs_restore:
            painter.restore()

        if self._is_completed() and not self._is_dimmed():
            self._draw_completed_check(painter, base)

    def _title_font(self, pt: int) -> QFont:
        """Build a title font at the given point size, respecting dimmed state."""
        f = QFont(theme.FONT_FAMILY, pt, QFont.Weight.Medium)
        if self._is_dimmed():
            f.setStrikeOut(True)
        return f

    def _make_title_font(self) -> QFont:
        return self._title_font(theme.FONT_CHIP_TITLE)

    def _draw_completed_check(self, painter: QPainter, base: QColor) -> None:
        """Draw a ✓ glyph at the top-right of the chip (full opacity, after restore)."""
        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H:
            return  # chip too small — opacity + desaturation alone signal completion
        glyph = "✓"
        pt = max(6, min(9, int(h * 0.35)))
        f = QFont(theme.FONT_FAMILY, pt, QFont.Weight.Bold)
        fm = QFontMetricsF(f)
        glyph_w = fm.horizontalAdvance(glyph)
        x = self._rect.right() - glyph_w - 4
        y = self._rect.top() + 2
        painter.save()
        painter.setFont(f)
        text_c = (
            _readable_text_color(base)
            if self._mode is ChipMode.BARS
            else QColor(theme.SUCCESS)
        )
        painter.setPen(text_c)
        painter.setClipping(False)
        painter.drawText(
            QRectF(x, y, glyph_w + 2, fm.height()),
            Qt.AlignmentFlag.AlignLeft,
            glyph,
        )
        painter.restore()

    # ── Bars mode ────────────────────────────────────────────────────────
    def _paint_bars_mode(self, painter: QPainter, base: QColor) -> None:
        body = self._rect.adjusted(0, 0, -1, -1)
        if self._is_needs_action():
            fill = QColor(base)
            fill.setAlphaF(0.55)
            painter.setBrush(fill)
            pen = QPen(base.darker(160), 1.2, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
        else:
            painter.setBrush(base)
            painter.setPen(QPen(base.darker(160), 0))
        painter.drawRoundedRect(body, 3, 3)

        text_color = _readable_text_color(base)

        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H:
            # Tier 0: no text. Tooltip carries info.
            self._draw_continuation_glyphs(painter, text_color)
            self._draw_overlap_badge(painter)
            return

        title_pt = theme.FONT_CHIP_TITLE
        prefix_pt = theme.FONT_CHIP_PREFIX

        pad_l = 3 if not self._continues_left else 10
        pad_r = 3 if not self._continues_right else 10
        text_x = self._rect.x() + pad_l
        text_w = max(8.0, self._rect.width() - pad_l - pad_r)

        title = self._event.summary or "(no title)"
        title_font = self._title_font(title_pt)
        title_fm = QFontMetricsF(title_font)

        painter.setPen(text_color)
        painter.setClipRect(QRectF(text_x, self._rect.y(), text_w, h - 1))

        if h >= theme.CHIP_MIN_PREFIX_H:
            # ── Tier 2/3: time on its own row, then title ──────────────────
            cursor_y = self._rect.y() + 1

            prefix = self._prefix_text()
            if prefix:
                pf = QFont(theme.FONT_FAMILY, prefix_pt)
                painter.setFont(pf)
                pfm = QFontMetricsF(pf)
                pen_color = QColor(text_color)
                pen_color.setAlphaF(0.85)
                painter.setPen(pen_color)
                painter.drawText(
                    QRectF(text_x, cursor_y, text_w, pfm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _ellipsize(painter, prefix, text_w),
                )
                cursor_y += pfm.height() - 1
                painter.setPen(text_color)

            # Location is only shown if the title fits completely alongside it.
            available = self._rect.bottom() - cursor_y - 1
            location = (self._event.location or "").strip()
            show_location = False
            loc_reserved = 0.0
            if location and h >= theme.CHIP_MIN_LOCATION_H:
                loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
                loc_fm = QFontMetricsF(loc_font)
                if h >= theme.CHIP_MIN_LOCATION_MULTILINE_H:
                    max_loc_h = loc_fm.height() * 2 + 2
                    bound = loc_fm.boundingRect(
                        QRectF(0, 0, text_w, max_loc_h),
                        int(Qt.TextFlag.TextWordWrap),
                        location,
                    )
                    needed_loc = min(bound.height(), max_loc_h)
                else:
                    needed_loc = loc_fm.height()
                title_bound = title_fm.boundingRect(
                    QRectF(0, 0, text_w, available),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
                    title,
                )
                if title_bound.height() + needed_loc <= available:
                    show_location = True
                    loc_reserved = needed_loc

            title_rect = QRectF(
                text_x,
                cursor_y,
                text_w,
                max(title_fm.height(), available - loc_reserved),
            )
            painter.setFont(title_font)
            _draw_tight_wrapped(painter, title, title_font, title_rect)

            if show_location:
                loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
                painter.setFont(loc_font)
                loc_fm = QFontMetricsF(loc_font)
                loc_color = QColor(text_color)
                loc_color.setAlphaF(0.80)
                painter.setPen(loc_color)
                loc_y = self._rect.bottom() - loc_reserved
                if h >= theme.CHIP_MIN_LOCATION_MULTILINE_H:
                    painter.drawText(
                        QRectF(text_x, loc_y, text_w, loc_reserved),
                        int(
                            Qt.AlignmentFlag.AlignLeft
                            | Qt.AlignmentFlag.AlignTop
                            | Qt.TextFlag.TextWordWrap
                        ),
                        location,
                    )
                else:
                    painter.drawText(
                        QRectF(text_x, loc_y, text_w, loc_fm.height()),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        _ellipsize(painter, location, text_w),
                    )

        elif h >= theme.CHIP_MIN_INLINE_TIME_H:
            # ── Tier 1b: time + title on one row, vertically centred ────────
            pf = QFont(theme.FONT_FAMILY, prefix_pt)
            pfm = QFontMetricsF(pf)
            inline_line_h = max(title_fm.height(), pfm.height())
            cursor_y = self._rect.y() + max(1.0, (h - inline_line_h) / 2)

            prefix = self._prefix_text()
            title_x = text_x
            remaining_w = text_w
            if prefix:
                prefix_str = prefix + "  "
                prefix_px = pfm.horizontalAdvance(prefix_str)
                pen_color = QColor(text_color)
                pen_color.setAlphaF(0.85)
                painter.setFont(pf)
                painter.setPen(pen_color)
                painter.drawText(
                    QRectF(text_x, cursor_y, min(prefix_px, text_w), pfm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    prefix_str,
                )
                title_x = text_x + prefix_px
                remaining_w = max(0.0, text_w - prefix_px)

            if remaining_w > 4.0:
                painter.setFont(title_font)
                painter.setPen(text_color)
                painter.drawText(
                    QRectF(title_x, cursor_y, remaining_w, title_fm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _ellipsize(painter, title, remaining_w),
                )

        else:
            # ── Tier 1a: title only, vertically centred ─────────────────────
            cursor_y = self._rect.y() + max(1.0, (h - title_fm.height()) / 2)
            painter.setFont(title_font)
            painter.drawText(
                QRectF(
                    text_x,
                    cursor_y,
                    text_w,
                    max(title_fm.height(), self._rect.bottom() - cursor_y - 1),
                ),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                _ellipsize(painter, title, text_w),
            )

        painter.setClipping(False)
        self._draw_recurrence_glyph(painter, text_color)
        self._draw_overlap_badge(painter)
        self._draw_continuation_glyphs(painter, text_color)

    # ── Text mode ─────────────────────────────────────────────────────────
    def _paint_text_mode(self, painter: QPainter, base: QColor) -> None:
        _bar_w = 3
        body = self._rect.adjusted(0, 0, -1, -1)
        painter.setBrush(QColor(theme.BG_SURFACE_ALT))
        if self._is_needs_action():
            outline_pen = QPen(base.darker(140), 1.0, Qt.PenStyle.DashLine)
            outline_pen.setCosmetic(True)
            painter.setPen(outline_pen)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(body, 2, 2)

        bar_rect = QRectF(
            self._rect.x() + 1, self._rect.y() + 1, _bar_w, self._rect.height() - 3
        )
        painter.setBrush(base)
        painter.drawRect(bar_rect)

        text_color = QColor(theme.TEXT_PRIMARY)
        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H:
            self._draw_overlap_badge(painter)
            return

        title_pt = theme.FONT_CHIP_TITLE
        prefix_pt = theme.FONT_CHIP_PREFIX

        pad_l = _bar_w + 5  # bar + 2 px gap + 3 px body pad
        pad_r = 3
        text_x = self._rect.x() + pad_l
        text_w = max(8.0, self._rect.width() - pad_l - pad_r)

        title = self._event.summary or "(no title)"
        title_font = self._title_font(title_pt)
        title_fm = QFontMetricsF(title_font)

        painter.setClipRect(QRectF(text_x, self._rect.y(), text_w, h - 1))

        if h >= theme.CHIP_MIN_PREFIX_H:
            # ── Tier 2/3: time on its own row, then title ──────────────────
            cursor_y = self._rect.y() + 1

            prefix = self._prefix_text()
            if prefix:
                pf = QFont(theme.FONT_FAMILY, prefix_pt)
                painter.setFont(pf)
                pfm = QFontMetricsF(pf)
                pen_color = QColor(text_color)
                pen_color.setAlphaF(0.85)
                painter.setPen(pen_color)
                painter.drawText(
                    QRectF(text_x, cursor_y, text_w, pfm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _ellipsize(painter, prefix, text_w),
                )
                cursor_y += pfm.height() - 1
                painter.setPen(text_color)

            # Location is only shown if the title fits completely alongside it.
            available = self._rect.bottom() - cursor_y - 1
            location = (self._event.location or "").strip()
            show_location = False
            loc_reserved = 0.0
            if location and h >= theme.CHIP_MIN_LOCATION_H:
                loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
                loc_fm = QFontMetricsF(loc_font)
                if h >= theme.CHIP_MIN_LOCATION_MULTILINE_H:
                    max_loc_h = loc_fm.height() * 2 + 2
                    bound = loc_fm.boundingRect(
                        QRectF(0, 0, text_w, max_loc_h),
                        int(Qt.TextFlag.TextWordWrap),
                        location,
                    )
                    needed_loc = min(bound.height(), max_loc_h)
                else:
                    needed_loc = loc_fm.height()
                title_bound = title_fm.boundingRect(
                    QRectF(0, 0, text_w, available),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
                    title,
                )
                if title_bound.height() + needed_loc <= available:
                    show_location = True
                    loc_reserved = needed_loc

            title_rect = QRectF(
                text_x,
                cursor_y,
                text_w,
                max(title_fm.height(), available - loc_reserved),
            )
            painter.setFont(title_font)
            painter.setPen(text_color)
            _draw_tight_wrapped(painter, title, title_font, title_rect)

            if show_location:
                loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
                painter.setFont(loc_font)
                loc_fm = QFontMetricsF(loc_font)
                loc_color = QColor(text_color)
                loc_color.setAlphaF(0.80)
                painter.setPen(loc_color)
                loc_y = self._rect.bottom() - loc_reserved
                if h >= theme.CHIP_MIN_LOCATION_MULTILINE_H:
                    painter.drawText(
                        QRectF(text_x, loc_y, text_w, loc_reserved),
                        int(
                            Qt.AlignmentFlag.AlignLeft
                            | Qt.AlignmentFlag.AlignTop
                            | Qt.TextFlag.TextWordWrap
                        ),
                        location,
                    )
                else:
                    painter.drawText(
                        QRectF(text_x, loc_y, text_w, loc_fm.height()),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        _ellipsize(painter, location, text_w),
                    )

        elif h >= theme.CHIP_MIN_INLINE_TIME_H:
            # ── Tier 1b: time + title on one row, vertically centred ────────
            pf = QFont(theme.FONT_FAMILY, prefix_pt)
            pfm = QFontMetricsF(pf)
            inline_line_h = max(title_fm.height(), pfm.height())
            cursor_y = self._rect.y() + max(1.0, (h - inline_line_h) / 2)

            prefix = self._prefix_text()
            title_x = text_x
            remaining_w = text_w
            if prefix:
                prefix_str = prefix + "  "
                prefix_px = pfm.horizontalAdvance(prefix_str)
                pen_color = QColor(text_color)
                pen_color.setAlphaF(0.85)
                painter.setFont(pf)
                painter.setPen(pen_color)
                painter.drawText(
                    QRectF(text_x, cursor_y, min(prefix_px, text_w), pfm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    prefix_str,
                )
                title_x = text_x + prefix_px
                remaining_w = max(0.0, text_w - prefix_px)

            if remaining_w > 4.0:
                painter.setFont(title_font)
                painter.setPen(text_color)
                painter.drawText(
                    QRectF(title_x, cursor_y, remaining_w, title_fm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _ellipsize(painter, title, remaining_w),
                )

        else:
            # ── Tier 1a: title only, vertically centred ─────────────────────
            cursor_y = self._rect.y() + max(1.0, (h - title_fm.height()) / 2)
            painter.setFont(title_font)
            painter.setPen(text_color)
            painter.drawText(
                QRectF(
                    text_x,
                    cursor_y,
                    text_w,
                    max(title_fm.height(), self._rect.bottom() - cursor_y - 1),
                ),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                _ellipsize(painter, title, text_w),
            )

        painter.setClipping(False)
        self._draw_recurrence_glyph(painter, text_color)
        self._draw_overlap_badge(painter)

    # ── Dot mode (Agenda children, very dense rows) ──────────────────────
    def _paint_dot(self, painter: QPainter, base: QColor) -> None:
        dot_d = 6
        cy = self._rect.center().y()
        dot_x = self._rect.x() + 4
        if self._is_needs_action():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            dot_pen = QPen(base, 1.5, Qt.PenStyle.DashLine)
            dot_pen.setCosmetic(True)
            painter.setPen(dot_pen)
        else:
            painter.setBrush(base)
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(dot_x, cy - dot_d / 2, dot_d, dot_d))

        text_x = dot_x + dot_d + 6
        text_w = max(8.0, self._rect.right() - text_x - 4)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        dot_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_TITLE)
        if self._is_dimmed():
            dot_font.setStrikeOut(True)
        painter.setFont(dot_font)
        painter.drawText(
            QRectF(text_x, self._rect.y(), text_w, self._rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            _ellipsize(painter, self._event.summary or "(no title)", text_w),
        )

    # ── Continuation arrows for multi-day spans ──────────────────────────
    def _draw_continuation_glyphs(self, painter: QPainter, fg: QColor) -> None:
        if not (self._continues_left or self._continues_right):
            return
        painter.setPen(fg)
        painter.setFont(
            QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX, QFont.Weight.Bold)
        )
        if self._continues_left:
            painter.drawText(
                QRectF(self._rect.x() + 1, self._rect.y(), 10, self._rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                theme.GLYPH_CONTINUES_LEFT,
            )
        if self._continues_right:
            painter.drawText(
                QRectF(self._rect.right() - 10, self._rect.y(), 9, self._rect.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                theme.GLYPH_CONTINUES_RIGHT,
            )

    # ── Recurrence indicator ─────────────────────────────────────────────
    def _draw_recurrence_glyph(self, painter: QPainter, text_color: QColor) -> None:
        if not (self._event.rrule or self._event.recurrence_id is not None):
            return
        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H or self._rect.width() < 18:
            return
        glyph_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX)
        painter.setFont(glyph_font)
        fm = QFontMetricsF(glyph_font)
        glyph = "↻"
        gw = fm.horizontalAdvance(glyph)
        gx = self._rect.right() - gw - 2
        gy = self._rect.bottom() - fm.height() - 1
        color = QColor(text_color)
        color.setAlphaF(0.70)
        painter.setPen(color)
        painter.setClipping(False)
        painter.drawText(
            QRectF(gx, gy, gw + 1, fm.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            glyph,
        )

    # ── Overlap density badge ─────────────────────────────────────────────
    def _draw_overlap_badge(self, painter: QPainter) -> None:
        if self._overlap_cols <= 1 or self._rect.width() >= _BADGE_CHIP_MAX_W:
            return
        badge_text = f"+{self._overlap_cols - 1}"
        badge_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX, QFont.Weight.Bold)
        painter.setFont(badge_font)
        fm = QFontMetricsF(badge_font)
        bw = fm.horizontalAdvance(badge_text) + 4
        bh = fm.height() + 2
        bx = self._rect.right() - bw - 1
        by = self._rect.y() + 1
        painter.setClipping(False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 120))
        painter.drawRoundedRect(QRectF(bx, by, bw, bh), 2, 2)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            QRectF(bx + 2, by + 1, bw - 4, bh - 2),
            Qt.AlignmentFlag.AlignCenter,
            badge_text,
        )

    # ── Interactivity ────────────────────────────────────────────────────
    def hoverEnterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hovered = False
        self.unsetCursor()
        self.update()
        super().hoverLeaveEvent(event)

    def hoverMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        # Show a vertical resize cursor near the top/bottom edge so users
        # discover the resize affordance. All-day chips and tiny timed chips
        # get the default cursor (body-drag-only).
        if self._can_edge_resize():
            local_y = event.pos().y()
            if local_y - self._rect.top() <= self.EDGE_RESIZE_PX:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                super().hoverMoveEvent(event)
                return
            if self._rect.bottom() - local_y <= self.EDGE_RESIZE_PX:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                super().hoverMoveEvent(event)
                return
        self.unsetCursor()
        super().hoverMoveEvent(event)

    def _can_edge_resize(self) -> bool:
        """True for timed chips tall enough to expose top/bottom edge zones.

        All-day chips (and very short timed chips) only support body-drag.
        We approximate "all-day" via show_time_prefix=False, matching how
        the views construct chips.
        """
        if not self._show_time_prefix:
            return False
        return self._rect.height() >= self.MIN_HEIGHT_FOR_EDGE_RESIZE

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # Decide initial drag mode based on which edge zone the press lands
        # in. We start "pending" for body presses — only convert to "move"
        # once the user moves past MOVE_THRESHOLD_PX.
        local_y = event.pos().y()
        if self._can_edge_resize():
            if local_y - self._rect.top() <= self.EDGE_RESIZE_PX:
                self._drag_mode = "resize_top"
            elif self._rect.bottom() - local_y <= self.EDGE_RESIZE_PX:
                self._drag_mode = "resize_bottom"
            else:
                self._drag_mode = "pending"
        else:
            self._drag_mode = "pending"
        self._press_scene_pos = QPointF(event.scenePos())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if self._drag_mode is None or self._press_scene_pos is None:
            super().mouseMoveEvent(event)
            return
        scene_pos = event.scenePos()
        # Promote a pending press to a real move once the cursor crosses
        # the threshold. Otherwise this is just a wobbly click.
        if self._drag_mode == "pending":
            dx = scene_pos.x() - self._press_scene_pos.x()
            dy = scene_pos.y() - self._press_scene_pos.y()
            if (dx * dx + dy * dy) ** 0.5 < self.MOVE_THRESHOLD_PX:
                event.accept()
                return
            self._drag_mode = "move"
        # Emit the drag progress so the owning view can update its preview.
        self.drag_progress.emit(self._event, self._drag_mode, scene_pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag_mode is None:
            super().mouseReleaseEvent(event)
            return
        mode = self._drag_mode
        scene_pos = event.scenePos()
        self._drag_mode = None
        self._press_scene_pos = None
        if mode == "pending":
            if self.contains(event.pos()):
                self.details_requested.emit(self._event)
        else:
            self.drag_committed.emit(self._event, mode, scene_pos)
        event.accept()

    def cancel_drag(self) -> None:
        """Called by the owning view when Escape is pressed mid-drag."""
        if self._drag_mode is None:
            return
        self._drag_mode = None
        self._press_scene_pos = None
        self.drag_cancelled.emit(self._event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()  # swallow — single-click already opened the details dialog
        else:
            super().mouseDoubleClickEvent(event)

    @override
    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        edit_act = menu.addAction("Edit…")
        menu.addSeparator()
        del_act = menu.addAction("Delete…")
        chosen = menu.exec(event.screenPos())
        if chosen is edit_act:
            self.edit_requested.emit(self._event)
        elif chosen is del_act:
            self.delete_requested.emit(self._event)
