"""LineCluster — dominant chip + right-side spine of thin colored bars."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsObject

from lilical.ui import theme
from lilical.ui.widgets.event_chip import ChipMode, EventChip


def _resolve_cluster_geometry(
    rect: QRectF,
    cluster_data: dict[str, Any],
    px_per_hour: float,
    color_map: dict[str, str | None],
    time_format: str,
    read_only_cal_ids: set[str],
) -> dict[str, Any]:
    """Compute geometry for a cluster.  Pure; no Qt side-effects."""
    events: list[dict[str, Any]] = cluster_data["events"]
    dominant_idx: int = cluster_data["dominant_index"]
    cluster_start_min: float = cluster_data["cluster_start_min"]

    total_w = rect.width()
    n_secondary = len(events) - 1
    px_per_min = px_per_hour / 60.0

    raw_spine_w = (
        n_secondary * (theme.CLUSTER_LINE_WIDTH_PX + theme.CLUSTER_LINE_GAP_PX) + 8
    )
    spine_w = min(raw_spine_w, total_w * theme.CLUSTER_SPINE_MAX_FRAC)
    chip_total_w = total_w - spine_w

    dom = events[dominant_idx]
    dom_start: float = dom["start_min"]
    dom_end: float = dom["end_min"]
    dom_payload = dom["payload"]
    dom_cal_id: str = dom["cal_id"]

    chip_y_local = (dom_start - cluster_start_min) * px_per_min
    chip_h = max(14.0, (dom_end - dom_start) * px_per_min)
    chip_rect = QRectF(0.0, chip_y_local, chip_total_w, chip_h)

    _tfmt = "%-I:%M %p" if time_format == "12h" else "%H:%M"
    dom_time_prefix = (
        dom_payload["start_dt"].strftime(_tfmt)
        if dom_payload.get("show_time_prefix") and dom_payload.get("start_dt")
        else None
    )

    bar_rects: list[tuple[QRectF, QColor]] = []
    sec_idx = 0
    for i, ev in enumerate(events):
        if i == dominant_idx:
            continue
        cal_id: str = ev["cal_id"]
        color_str = color_map.get(cal_id) or theme.CHIP_FALLBACK
        qc = QColor(color_str)
        color = qc if qc.isValid() else QColor(theme.CHIP_FALLBACK)
        bar_x_local = (
            chip_total_w
            + 4
            + sec_idx * (theme.CLUSTER_LINE_WIDTH_PX + theme.CLUSTER_LINE_GAP_PX)
        )
        bar_y_local = (ev["start_min"] - cluster_start_min) * px_per_min
        bar_h = max(2.0, (ev["end_min"] - ev["start_min"]) * px_per_min)
        bar_rects.append(
            (
                QRectF(bar_x_local, bar_y_local, theme.CLUSTER_LINE_WIDTH_PX, bar_h),
                color,
            )
        )
        sec_idx += 1

    return {
        "spine_w": spine_w,
        "chip_rect": chip_rect,
        "chip_h": chip_h,
        "dom_event": dom_payload["event"],
        "dom_cal_id": dom_cal_id,
        "dom_color": color_map.get(dom_cal_id),
        "dom_time_prefix": dom_time_prefix,
        "dom_show_time_prefix": dom_payload.get("show_time_prefix", True),
        "dom_inst_dtstart": dom_payload.get("instance_dtstart"),
        "dom_completed": dom_payload.get("completed", False),
        "dom_inst_key": dom_payload.get("inst_key"),
        "dom_read_only": dom_cal_id in read_only_cal_ids,
        "bar_rects": bar_rects,
    }


class LineCluster(QGraphicsObject):
    """Renders a dense overlap cluster as one dominant chip + thin spine bars.

    The dominant event occupies most of the column width as a normal TEXT-mode
    chip.  Every other event in the cluster is represented by a 4 px wide
    colored vertical bar whose vertical extent maps to the event's real
    start/end times against the time grid.

    Signals:
        hovered: emitted on hoverEnterEvent with the raw cluster events list.
        hover_left: emitted on hoverLeaveEvent.
        spine_clicked: emitted when the user clicks the spine (bar) area.
    """

    hovered = Signal(object)        # list[dict] — cluster event entries
    hover_left = Signal()
    spine_clicked = Signal(object)  # list[dict] — cluster event entries

    def __init__(
        self,
        rect: QRectF,
        cluster_data: dict[str, Any],
        px_per_hour: float,
        *,
        calendar_color_map: dict[str, str | None] | None = None,
        time_format: str = "24h",
        read_only_cal_ids: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._rect = rect
        self._cluster_data = cluster_data
        self._time_format = time_format

        color_map = calendar_color_map or {}
        ro = read_only_cal_ids or set()

        geo = _resolve_cluster_geometry(  # noqa: E501
            rect, cluster_data, px_per_hour, color_map, time_format, ro
        )

        self._spine_w: float = geo["spine_w"]
        self._bar_rects: list[tuple[QRectF, QColor]] = geo["bar_rects"]

        self._chip = EventChip(
            geo["dom_event"],
            geo["chip_rect"],
            calendar_color=geo["dom_color"],
            mode=ChipMode.TEXT,
            show_time_prefix=geo["dom_show_time_prefix"],
            time_prefix=geo["dom_time_prefix"],
            time_format=time_format,
            instance_dtstart=geo["dom_inst_dtstart"],
            completed=geo["dom_completed"],
            inst_key=geo["dom_inst_key"],
            read_only=geo["dom_read_only"],
        )
        self._chip.setParentItem(self)
        self._chip.setAcceptHoverEvents(False)

        self.setAcceptHoverEvents(True)
        self._hovered: bool = False

    @property
    def chip(self) -> EventChip:
        """The embedded dominant-event chip (wire its signals in the view)."""
        return self._chip

    @property
    def cluster_events(self) -> list[dict[str, Any]]:
        return self._cluster_data["events"]  # type: ignore[return-value]

    def update_layout(
        self,
        rect: QRectF,
        cluster_data: dict[str, Any],
        px_per_hour: float,
        *,
        calendar_color_map: dict[str, str | None] | None = None,
        time_format: str = "24h",
        read_only_cal_ids: set[str] | None = None,
    ) -> None:
        """Update geometry in-place; chip object is reused (signals preserved)."""
        self.prepareGeometryChange()
        self._rect = rect
        self._cluster_data = cluster_data
        self._time_format = time_format

        color_map = calendar_color_map or {}
        ro = read_only_cal_ids or set()

        geo = _resolve_cluster_geometry(  # noqa: E501
            rect, cluster_data, px_per_hour, color_map, time_format, ro
        )

        self._spine_w = geo["spine_w"]
        self._bar_rects = geo["bar_rects"]

        self._chip.update_event_data(
            geo["dom_event"],
            completed=geo["dom_completed"],
            inst_key=geo["dom_inst_key"],
        )
        self._chip.update_layout(
            geo["chip_rect"],
            calendar_color=geo["dom_color"],
            time_prefix=geo["dom_time_prefix"],
            show_time_prefix=geo["dom_show_time_prefix"],
            instance_dtstart=geo["dom_inst_dtstart"],
        )
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self._rect.width(), self._rect.height())

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Separator between chip area and spine.
        sep_x = self._rect.width() - self._spine_w
        pen = QPen(QColor(theme.BORDER), 0.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(int(sep_x), 0, int(sep_x), int(self._rect.height()))

        # Secondary event bars.
        painter.setPen(Qt.PenStyle.NoPen)
        for bar_rect, color in self._bar_rects:
            painter.setBrush(color)
            painter.drawRect(bar_rect)

    def hoverEnterEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._hovered:
            self._hovered = True
            self.hovered.emit(self._cluster_data["events"])
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: ANN001, N802
        pos = event.scenePos()
        if not self.sceneBoundingRect().contains(pos):
            self._hovered = False
            self.hover_left.emit()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            chip_w = self._rect.width() - self._spine_w
            if event.pos().x() > chip_w:
                self.spine_clicked.emit(self._cluster_data["events"])
                event.accept()
                return
        super().mousePressEvent(event)
