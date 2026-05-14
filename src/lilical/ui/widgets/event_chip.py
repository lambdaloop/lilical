from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QGraphicsObject, QGraphicsSceneContextMenuEvent, QMenu

from lilical.ui import theme

if TYPE_CHECKING:
    from lilical.models.event import Event


class ChipMode(Enum):
    BARS = "bars"   # solid fill, title overlaid in contrasting text
    TEXT = "text"   # 3 px coloured left bar, neutral background
    DOT = "dot"     # tiny coloured dot + title, for ultra-tight rows


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


def _ellipsize(painter: QPainter, text: str, max_w: float) -> str:
    """Single-line ellipsize using the painter's current font."""
    fm = QFontMetricsF(painter.font())
    return fm.elidedText(text, Qt.TextElideMode.ElideRight, max_w)


def _coerce_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _format_time_prefix_from(event: "Event") -> str | None:
    """Fallback time-prefix derived from the master Event (not instance-aware)."""
    if event.all_day:
        return None
    start = _coerce_dt(event.dtstart)
    if start is None:
        return None
    s = start.astimezone().strftime("%H:%M")
    return s


class EventChip(QGraphicsObject):
    """Colored rectangle representing one calendar event in a graphics view.

    Rendering tiers (height-based, per UI-SPEC §1.5 + §4.2):
        h <  14 px : solid fill, no text (tooltip carries title)
        h >= 14 px : title only, top-aligned, single line
        h >= 24 px : time prefix + title (title wraps)
        h >= 38 px : + location (if event.location set)
    """

    edit_requested = Signal(object)    # emits Event
    delete_requested = Signal(object)  # emits Event
    # Drag signals: see docstring for the chip-drag state machine.
    # Payload: (event, mode, scene_pos). mode ∈ {"move", "resize_top",
    # "resize_bottom"}.
    drag_progress = Signal(object, str, "QPointF")
    drag_committed = Signal(object, str, "QPointF")
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
        continues_left: bool = False,
        continues_right: bool = False,
    ) -> None:
        super().__init__()
        self._event = event
        self._rect = rect
        self._calendar_color = calendar_color
        self._mode = mode
        self._show_time_prefix = show_time_prefix
        self._time_prefix = time_prefix
        self._continues_left = continues_left
        self._continues_right = continues_right
        self._hovered = False
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
        return _format_time_prefix_from(self._event)

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
        return "\n".join(parts)

    def _is_dimmed(self) -> bool:
        """An event is dimmed when it's cancelled or the user declined."""
        status = (self._event.status or "").upper()
        if status == "CANCELLED":
            return True
        sr = (self._event.self_response or "").upper()
        return sr == "DECLINED"

    def _dim_reason(self) -> str | None:
        status = (self._event.status or "").upper()
        if status == "CANCELLED":
            return "cancelled"
        sr = (self._event.self_response or "").upper()
        if sr == "DECLINED":
            return "declined"
        return None

    @override
    def boundingRect(self) -> QRectF:
        return self._rect

    @override
    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        base = _resolve_color(self._event.color, self._calendar_color)
        if self._hovered:
            base = base.lighter(115)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Cancelled events + events the user declined are drawn at reduced
        # opacity. Title also gets a strikethrough (see _make_title_font).
        if self._is_dimmed():
            painter.save()
            painter.setOpacity(0.45)

        if self._mode is ChipMode.DOT:
            self._paint_dot(painter, base)
        elif self._mode is ChipMode.TEXT:
            self._paint_text_mode(painter, base)
        else:
            # BARS mode (default)
            self._paint_bars_mode(painter, base)

        if self._is_dimmed():
            painter.restore()

    def _make_title_font(self) -> QFont:
        f = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_TITLE, QFont.Weight.Medium)
        if self._is_dimmed():
            f.setStrikeOut(True)
        return f

    # ── Bars mode ────────────────────────────────────────────────────────
    def _paint_bars_mode(self, painter: QPainter, base: QColor) -> None:
        painter.setBrush(base)
        painter.setPen(QPen(base.darker(160), 0))
        body = self._rect.adjusted(0, 0, -1, -1)
        painter.drawRoundedRect(body, 3, 3)

        text_color = _readable_text_color(base)

        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H:
            # Tier 0: no text. Tooltip carries info.
            self._draw_continuation_glyphs(painter, text_color)
            return

        # Layout: 4 px left/right padding, 2 px top padding.
        pad_l = 4 if not self._continues_left else 10
        pad_r = 4 if not self._continues_right else 10
        text_x = self._rect.x() + pad_l
        text_w = max(8.0, self._rect.width() - pad_l - pad_r)
        cursor_y = self._rect.y() + 2

        painter.setPen(text_color)
        painter.setClipRect(
            QRectF(text_x, self._rect.y() + 1, text_w, h - 2)
        )

        # Time prefix on its own line if we have vertical room.
        has_prefix = False
        if h >= theme.CHIP_MIN_PREFIX_H:
            prefix = self._prefix_text()
            if prefix:
                f = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX)
                painter.setFont(f)
                fm = QFontMetricsF(f)
                # 80% opacity per UI-SPEC §1.3.
                pen_color = QColor(text_color)
                pen_color.setAlphaF(0.8)
                painter.setPen(pen_color)
                painter.drawText(
                    QRectF(text_x, cursor_y, text_w, fm.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _ellipsize(painter, prefix, text_w),
                )
                cursor_y += fm.height()
                has_prefix = True
                painter.setPen(text_color)

        # Title block — wraps if there's room.
        title = self._event.summary or "(no title)"
        title_font = self._make_title_font()
        painter.setFont(title_font)
        title_fm = QFontMetricsF(title_font)

        # Reserve space at the bottom for the location line if it'll fit.
        loc_reserved = 0.0
        location = (self._event.location or "").strip()
        show_location = (
            location and h >= theme.CHIP_MIN_LOCATION_H
        )
        if show_location:
            loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
            loc_fm = QFontMetricsF(loc_font)
            loc_reserved = loc_fm.height() + 1

        title_rect = QRectF(
            text_x,
            cursor_y,
            text_w,
            max(title_fm.height(), self._rect.bottom() - cursor_y - 2 - loc_reserved),
        )

        if h < theme.CHIP_MIN_PREFIX_H:
            # Tier 1 (single-line title): ellipsize, no wrap.
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                _ellipsize(painter, title, text_w),
            )
        else:
            # Tier 2/3: wrap; Qt will auto-clip to the rect.
            painter.drawText(
                title_rect,
                int(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignTop
                    | Qt.TextFlag.TextWordWrap
                ),
                title,
            )

        # Location line at the bottom.
        if show_location:
            loc_font = QFont(theme.FONT_FAMILY, theme.FONT_CHIP_LOCATION)
            painter.setFont(loc_font)
            loc_fm = QFontMetricsF(loc_font)
            loc_color = QColor(text_color)
            loc_color.setAlphaF(0.7)
            painter.setPen(loc_color)
            loc_y = self._rect.bottom() - loc_fm.height() - 2
            painter.drawText(
                QRectF(text_x, loc_y, text_w, loc_fm.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _ellipsize(painter, location, text_w),
            )

        painter.setClipping(False)
        self._draw_continuation_glyphs(painter, text_color)
        _ = has_prefix  # silence unused-var lint

    # ── Text mode (Month-view "Text" toggle) ─────────────────────────────
    def _paint_text_mode(self, painter: QPainter, base: QColor) -> None:
        # 3 px coloured left bar, neutral background, primary text colour.
        body = self._rect.adjusted(0, 0, -1, -1)
        painter.setBrush(QColor(theme.BG_SURFACE_ALT))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(body, 2, 2)

        bar_rect = QRectF(self._rect.x() + 1, self._rect.y() + 1, 3, self._rect.height() - 3)
        painter.setBrush(base)
        painter.drawRect(bar_rect)

        text_color = QColor(theme.TEXT_PRIMARY)
        h = self._rect.height()
        if h < theme.CHIP_MIN_TITLE_H:
            return

        pad_l = 8  # leave room for the 3 px bar + spacing
        pad_r = 4
        text_x = self._rect.x() + pad_l
        text_w = max(8.0, self._rect.width() - pad_l - pad_r)
        title = self._event.summary or "(no title)"

        painter.setPen(text_color)
        painter.setFont(self._make_title_font())
        painter.setClipRect(QRectF(text_x, self._rect.y(), text_w, h))

        prefix = self._prefix_text()
        if prefix:
            title = f"{prefix}  {title}"
        painter.drawText(
            QRectF(text_x, self._rect.y() + 1, text_w, h - 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            _ellipsize(painter, title, text_w),
        )
        painter.setClipping(False)

    # ── Dot mode (Agenda children, very dense rows) ──────────────────────
    def _paint_dot(self, painter: QPainter, base: QColor) -> None:
        painter.setBrush(base)
        painter.setPen(Qt.PenStyle.NoPen)
        dot_d = 6
        cy = self._rect.center().y()
        dot_x = self._rect.x() + 4
        painter.drawEllipse(QRectF(dot_x, cy - dot_d / 2, dot_d, dot_d))

        text_x = dot_x + dot_d + 6
        text_w = max(8.0, self._rect.right() - text_x - 4)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        # Dot-mode uses regular (non-Medium) weight — strikethrough still
        # applies when dimmed.
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
        painter.setFont(QFont(theme.FONT_FAMILY, theme.FONT_CHIP_PREFIX, QFont.Weight.Bold))
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

    # ── Interactivity ────────────────────────────────────────────────────
    def hoverEnterEvent(self, event) -> None:  # noqa: ANN001
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: ANN001
        self._hovered = False
        self.unsetCursor()
        self.update()
        super().hoverLeaveEvent(event)

    def hoverMoveEvent(self, event) -> None:  # noqa: ANN001
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

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
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

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
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

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton or self._drag_mode is None:
            super().mouseReleaseEvent(event)
            return
        mode = self._drag_mode
        scene_pos = event.scenePos()
        self._drag_mode = None
        self._press_scene_pos = None
        if mode == "pending":
            # Treat as a single click — preserves the previous behaviour.
            if self.contains(event.pos()):
                self.edit_requested.emit(self._event)
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

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        # Treat a double-click as another single-click — the dialog is modal,
        # so a second emit during the open dialog is harmless. This keeps the
        # behaviour predictable for users who still double-click out of habit.
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self._event)
            event.accept()
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
