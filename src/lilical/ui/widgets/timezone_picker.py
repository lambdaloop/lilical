"""Compact toolbar control for choosing the app-wide display timezone.

A narrow `QToolButton` showing e.g. "Los Angeles · PDT" that opens a popup with
a search field over every IANA zone.

Why not a `QCompleter` on an editable `QComboBox`: see the docstring on
`_CompletionPopup` in `contact_completer.py` — `QCompleter` interacted badly
with model resets here. Why not reuse `_CompletionPopup` itself: it is typed to
`Contact`, and more importantly it deliberately avoids taking focus
(`WA_ShowWithoutActivating` + a focus proxy back to a `QLineEdit` in the main
window). This popup owns its search field, so it needs real keyboard focus.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import override
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QKeyEvent,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.ui import theme
from lilical.utils.timezone import (
    display_tz_name,
    iana_zones,
    local_iana_tz,
    zone_exists,
)

# Matches "utc+5", "gmt-03:30", "+0530", "-8" — used to search zones by offset.
_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-]?)(\d{1,2}):?(\d{2})?$")

# Zone-name prefixes that are aliases or non-geographic, demoted in ranking so
# the canonical Region/City form wins ("US/Pacific" below "America/Los_Angeles").
_LEGACY_PREFIXES = ("etc/", "systemv/", "us/", "canada/", "brazil/", "mexico/")

_MINUS = "−"  # U+2212, aligns with digits better than a hyphen
_MIDDOT = "·"

_POPUP_W = 300
_POPUP_H = 340
# The button prefers 110px — enough for "Los Angeles · PDT" — but must be able
# to shrink. In Week view the toolbar's content already totals ~1240px at the
# default 1200px window, so a rigidly fixed width would push the inspector and
# settings buttons into QToolBar's ">>" overflow menu. It elides instead.
_BUTTON_W = 110
_BUTTON_MIN_W = 62
_MAX_RESULTS = 200


# ── Pure helpers (no Qt — the test seam) ──────────────────────────────────


def zone_city(name: str) -> str:
    """Human-facing leaf of a zone name: America/Los_Angeles -> Los Angeles."""
    return name.rsplit("/", 1)[-1].replace("_", " ")


def zone_offset_minutes(name: str, at: datetime | None = None) -> int:
    """UTC offset of `name` in minutes, 0 if the zone can't be resolved."""
    try:
        moment = (at or datetime.now()).astimezone(ZoneInfo(name))
    except Exception:
        return 0
    off = moment.utcoffset()
    return 0 if off is None else int(off.total_seconds() // 60)


def zone_offset_label(name: str, at: datetime | None = None) -> str:
    """"UTC-07:00" / "UTC+05:30"."""
    minutes = zone_offset_minutes(name, at)
    sign = _MINUS if minutes < 0 else "+"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def zone_abbrev(name: str, at: datetime | None = None) -> str:
    """Zone abbreviation ("PDT", "CEST").

    Falls back to the offset label when the platform gives a numeric or empty
    `%Z` — Etc/GMT+5 yields "-05", which reads as noise on a button.
    """
    try:
        moment = (at or datetime.now()).astimezone(ZoneInfo(name))
    except Exception:
        return zone_offset_label(name, at)
    abbrev = moment.strftime("%Z")
    if not abbrev or abbrev[0].isdigit() or abbrev[0] in "+-":
        return zone_offset_label(name, at)
    return abbrev


def zone_button_label(name: str, at: datetime | None = None) -> str:
    """"Los Angeles · PDT" — what the toolbar button shows."""
    return f"{zone_city(name)} {_MIDDOT} {zone_abbrev(name, at)}"


def zone_row_label(name: str) -> str:
    """"Los Angeles — America/Los_Angeles" — what a popup row shows."""
    city = zone_city(name)
    return city if city == name else f"{city} — {name}"


def _parse_offset_query(q: str) -> int | None:
    """Minutes for an offset-shaped query, else None."""
    m = _OFFSET_RE.match(q.replace(" ", ""))
    if not m:
        return None
    sign, hh, mm = m.groups()
    minutes = int(hh) * 60 + int(mm or 0)
    return -minutes if sign == "-" else minutes


def _is_legacy(name: str) -> bool:
    lower = name.casefold()
    return lower.startswith(_LEGACY_PREFIXES) or "/" not in name


def filter_zones(
    query: str,
    zones: Sequence[str],
    *,
    abbrevs: Mapping[str, str] | None = None,
    offsets: Mapping[str, int] | None = None,
    current: str | None = None,
    system: str | None = None,
    limit: int = _MAX_RESULTS,
) -> list[str]:
    """Zones matching `query`, best first.

    `abbrevs` and `offsets` are injectable so tests don't depend on the current
    DST state. Matching folds `_` and `/` to spaces, so one substring rule
    covers city-only ("denver"), region+city ("america los") and
    underscore/space equivalence.
    """
    pinned = [z for z in dict.fromkeys([current, system]) if z and z in zones]
    # Fold the query the same way as the haystack so "us/pacific",
    # "us pacific" and "us_pacific" all behave alike.
    q = query.strip().casefold().replace("_", " ").replace("/", " ")

    if not q:
        rest = [z for z in zones if z not in pinned]
        return (pinned + rest)[:limit]

    target_offset = _parse_offset_query(q)
    scored: list[tuple[int, bool, int, str]] = []

    for name in zones:
        hay = name.casefold().replace("_", " ").replace("/", " ")
        city = zone_city(name).casefold()

        rank: int | None = None
        if city == q:
            rank = 0
        elif city.startswith(q):
            rank = 1
        elif any(w.startswith(q) for w in city.split()):
            rank = 2
        elif hay.startswith(q):
            rank = 3
        elif (abbrevs is not None and abbrevs.get(name, "").casefold() == q) or (
            target_offset is not None
            and offsets is not None
            and offsets.get(name) == target_offset
        ):
            rank = 4
        elif q in hay:
            rank = 5

        if rank is None:
            continue
        scored.append((rank, _is_legacy(name), len(name), name))

    # Legacy sorts ahead of rank, not merely as a tiebreak within it: "pacific"
    # is the exact city of the US/Pacific alias, which would otherwise outrank
    # the canonical Pacific/Auckland. Aliases should always come last.
    scored.sort(key=lambda t: (t[1], t[0], t[2], t[3]))
    ordered = [t[3] for t in scored]

    # Pin current/system when they matched at all, so the zone you are in stays
    # reachable without scrolling.
    head = [z for z in pinned if z in ordered]
    tail = [z for z in ordered if z not in head]
    return (head + tail)[:limit]


def _build_abbrev_offset_maps(
    zones: Sequence[str], at: datetime | None = None
) -> tuple[dict[str, str], dict[str, int]]:
    moment = at or datetime.now()
    abbrevs: dict[str, str] = {}
    offsets: dict[str, int] = {}
    for z in zones:
        abbrevs[z] = zone_abbrev(z, moment)
        offsets[z] = zone_offset_minutes(z, moment)
    return abbrevs, offsets


# ── Widgets ───────────────────────────────────────────────────────────────


class _ZoneRowDelegate(QStyledItemDelegate):
    """Draws the zone name left and its UTC offset right-aligned.

    The name is elided against the offset column rather than drawn full width,
    otherwise long entries ("New York — America/New_York") run underneath it.
    """

    @override
    def paint(self, painter, option, index) -> None:  # noqa: ANN001
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        offset = index.data(Qt.ItemDataRole.UserRole + 1) or ""

        # Let the style draw the background/selection, but not the text — we
        # place that ourselves so it can be clipped to the name column.
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget
        )

        fm = QFontMetrics(opt.font)
        offset_w = (fm.horizontalAdvance(offset) + 12) if offset else 0
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)

        painter.save()
        name_rect = opt.rect.adjusted(6, 0, -(offset_w + 6), 0)
        painter.setPen(
            opt.palette.color(QPalette.ColorRole.HighlightedText)
            if selected
            else opt.palette.color(QPalette.ColorRole.Text)
        )
        painter.drawText(
            name_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm.elidedText(text, Qt.TextElideMode.ElideRight, name_rect.width()),
        )
        if offset:
            if not selected:
                painter.setPen(QColor(theme.TEXT_SECONDARY))
            painter.drawText(
                opt.rect.adjusted(0, 0, -8, 0),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                offset,
            )
        painter.restore()


class _ZoneSearchEdit(QLineEdit):
    """Search field that drives the results list with the arrow keys."""

    committed = Signal()
    dismissed = Signal()

    def __init__(self, results: QListWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._results = results
        self.setPlaceholderText("Search timezones…")
        self.setClearButtonEnabled(True)

    @override
    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            delta = 1 if key == Qt.Key.Key_Down else -1
            count = self._results.count()
            if count:
                row = max(0, min(count - 1, self._results.currentRow() + delta))
                self._results.setCurrentRow(row)
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.committed.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.dismissed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _ZonePopup(QFrame):
    """Search field + filtered zone list, shown under the picker button."""

    zone_picked = Signal(str)

    def __init__(self, anchor: QWidget) -> None:
        super().__init__(anchor)
        self._anchor = anchor
        self.setObjectName("tz-popup")
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setFixedSize(_POPUP_W, _POPUP_H)

        self._results = QListWidget(self)
        self._results.setObjectName("tz-list")
        self._results.setItemDelegate(_ZoneRowDelegate(self._results))
        self._results.setUniformItemSizes(True)
        self._results.itemClicked.connect(self._commit_item)

        self._search = _ZoneSearchEdit(self._results, self)
        self._search.textChanged.connect(self._refilter)
        self._search.committed.connect(self._commit_current)
        self._search.dismissed.connect(self.close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self._search)
        layout.addWidget(self._results, 1)

        self._zones = list(iana_zones())
        # ~600 strftime calls; a few ms, paid once per open so DST changes and
        # offset search stay correct without a stale cache.
        self._abbrevs, self._offsets = _build_abbrev_offset_maps(self._zones)

    def open_at_anchor(self) -> None:
        pos = self._anchor.mapToGlobal(self._anchor.rect().bottomLeft())
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            avail: QRect = screen.availableGeometry()
            # The button sits near the right edge of the toolbar, so the popup
            # would otherwise hang off-screen.
            x = min(pos.x(), avail.right() - _POPUP_W)
            x = max(x, avail.left())
            y = pos.y()
            if y + _POPUP_H > avail.bottom():
                y = max(avail.top(), pos.y() - _POPUP_H - self._anchor.height())
            pos.setX(x)
            pos.setY(y)
        self.move(pos)
        self._search.clear()  # also triggers _refilter
        self._refilter("")
        self.show()
        self._search.setFocus(Qt.FocusReason.PopupFocusReason)

    def _refilter(self, text: str) -> None:
        matches = filter_zones(
            text,
            self._zones,
            abbrevs=self._abbrevs,
            offsets=self._offsets,
            current=display_tz_name(),
            system=local_iana_tz(),
        )
        self._results.clear()
        for name in matches:
            item = QListWidgetItem(zone_row_label(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, zone_offset_label(name))
            self._results.addItem(item)
        if self._results.count():
            self._results.setCurrentRow(0)

    def _commit_current(self) -> None:
        item = self._results.currentItem()
        if item is not None:
            self._commit_item(item)

    def _commit_item(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        self.close()
        if name:
            self.zone_picked.emit(name)


class TimezonePicker(QToolButton):
    """Toolbar button showing the active display zone; click to change it."""

    zone_changed = Signal(str)  # IANA name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tz-picker")
        self.setCheckable(True)
        self.setMinimumWidth(_BUTTON_MIN_W)
        self.setMaximumWidth(_BUTTON_W)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.clicked.connect(self._open_popup)

        self._zone_name = display_tz_name()
        self._popup: _ZonePopup | None = None
        self.refresh_label()

        # "PDT" becomes "PST" at a DST boundary; cheap enough to just re-derive.
        self._label_timer = QTimer(self)
        self._label_timer.setInterval(15 * 60 * 1000)
        self._label_timer.timeout.connect(self.refresh_label)
        self._label_timer.start()

    # ── API ───────────────────────────────────────────────────────────────

    def zone_name(self) -> str:
        return self._zone_name

    def set_zone_name(self, name: str) -> None:
        """Update the shown zone. Does not emit `zone_changed`."""
        if not zone_exists(name):
            return
        self._zone_name = name
        self.refresh_label()

    def refresh_label(self) -> None:
        # Degrade "Los Angeles · PDT" -> "PDT" rather than eliding mid-word to
        # "Los Angeles · P…", which reads as broken. Week view squeezes the
        # toolbar enough for this to matter.
        fm = QFontMetrics(self.font())
        avail = max(20, self.width() - 14)
        for candidate in (
            zone_button_label(self._zone_name),
            zone_abbrev(self._zone_name),
        ):
            if fm.horizontalAdvance(candidate) <= avail:
                self.setText(candidate)
                break
        else:
            self.setText(
                fm.elidedText(
                    zone_abbrev(self._zone_name), Qt.TextElideMode.ElideRight, avail
                )
            )
        self.setToolTip(
            f"Display timezone: {self._zone_name}\n"
            f"{zone_abbrev(self._zone_name)}  {zone_offset_label(self._zone_name)}"
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _open_popup(self) -> None:
        if self._popup is None:
            self._popup = _ZonePopup(self)
            self._popup.zone_picked.connect(self._on_picked)
            self._popup.destroyed.connect(lambda: setattr(self, "_popup", None))
        self._popup.open_at_anchor()
        self.setChecked(True)

    def _on_picked(self, name: str) -> None:
        self.setChecked(False)
        if name == self._zone_name:
            return
        self.set_zone_name(name)
        self.zone_changed.emit(name)

    @override
    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(_BUTTON_W)
        return hint

    @override
    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self.refresh_label()

    @override
    def changeEvent(self, event) -> None:  # noqa: ANN001
        # UI_SCALE changes the app font but not our fixed width, so the elision
        # has to be recomputed.
        if event.type() == QEvent.Type.FontChange:
            self.refresh_label()
        super().changeEvent(event)
