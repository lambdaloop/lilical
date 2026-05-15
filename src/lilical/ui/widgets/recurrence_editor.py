"""Inline RRULE editor widget for EventDialog."""
from __future__ import annotations

import re
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


_FREQ_LABELS = ["None", "Daily", "Weekly", "Monthly", "Yearly"]
_FREQ_RRULE = {"Daily": "DAILY", "Weekly": "WEEKLY", "Monthly": "MONTHLY", "Yearly": "YEARLY"}
_RRULE_FREQ = {v: k for k, v in _FREQ_RRULE.items()}

_BYDAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_BYDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


class RecurrenceEditor(QWidget):
    """Compact RRULE editor.

    Call `value()` to get the current RRULE string (None if "None" frequency),
    and `set_value(rrule)` to load an existing rule.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # ── Row 1: Frequency + Interval ───────────────────────────────────
        row1 = QHBoxLayout()
        self._freq_combo = QComboBox()
        self._freq_combo.addItems(_FREQ_LABELS)
        row1.addWidget(QLabel("Repeat:"))
        row1.addWidget(self._freq_combo)

        row1.addWidget(QLabel("every"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 999)
        self._interval_spin.setValue(1)
        row1.addWidget(self._interval_spin)
        self._interval_unit_label = QLabel("")
        row1.addWidget(self._interval_unit_label)
        row1.addStretch()
        outer.addLayout(row1)

        # ── Row 2: BYDAY checkboxes (weekly only) ─────────────────────────
        self._byday_row = QWidget()
        byday_layout = QHBoxLayout(self._byday_row)
        byday_layout.setContentsMargins(0, 0, 0, 0)
        byday_layout.addWidget(QLabel("On:"))
        self._byday_checks: list[QCheckBox] = []
        for label in _BYDAY_LABELS:
            cb = QCheckBox(label)
            byday_layout.addWidget(cb)
            self._byday_checks.append(cb)
        byday_layout.addStretch()
        self._byday_row.setVisible(False)
        outer.addWidget(self._byday_row)

        # ── Row 3: End condition ──────────────────────────────────────────
        self._end_row = QWidget()
        end_layout = QHBoxLayout(self._end_row)
        end_layout.setContentsMargins(0, 0, 0, 0)
        end_layout.addWidget(QLabel("End:"))

        self._rb_never = QRadioButton("Never")
        self._rb_never.setChecked(True)
        self._rb_until = QRadioButton("On date")
        self._rb_count = QRadioButton("After")

        self._until_edit = QDateEdit()
        self._until_edit.setDisplayFormat("yyyy-MM-dd")
        self._until_edit.setCalendarPopup(True)
        from datetime import datetime, timezone
        one_year = date.today().replace(year=date.today().year + 1)
        self._until_edit.setDate(
            self._until_edit.minimumDate().__class__(one_year.year, one_year.month, one_year.day)
        )
        self._until_edit.setEnabled(False)

        self._count_spin = QSpinBox()
        self._count_spin.setRange(1, 9999)
        self._count_spin.setValue(4)
        self._count_spin.setEnabled(False)
        self._count_label = QLabel("occurrences")

        end_layout.addWidget(self._rb_never)
        end_layout.addWidget(self._rb_until)
        end_layout.addWidget(self._until_edit)
        end_layout.addWidget(self._rb_count)
        end_layout.addWidget(self._count_spin)
        end_layout.addWidget(self._count_label)
        end_layout.addStretch()
        self._end_row.setVisible(False)
        outer.addWidget(self._end_row)

        # ── Signal wiring ─────────────────────────────────────────────────
        self._freq_combo.currentTextChanged.connect(self._on_freq_changed)
        self._rb_never.toggled.connect(self._on_end_toggled)
        self._rb_until.toggled.connect(self._on_end_toggled)
        self._rb_count.toggled.connect(self._on_end_toggled)

    def _on_freq_changed(self, text: str) -> None:
        has_freq = text != "None"
        self._end_row.setVisible(has_freq)
        self._interval_spin.setVisible(has_freq)
        self._interval_unit_label.setVisible(has_freq)
        self._byday_row.setVisible(has_freq and text == "Weekly")
        unit_map = {"Daily": "day(s)", "Weekly": "week(s)", "Monthly": "month(s)", "Yearly": "year(s)"}
        self._interval_unit_label.setText(unit_map.get(text, ""))

    def _on_end_toggled(self, _: bool) -> None:
        self._until_edit.setEnabled(self._rb_until.isChecked())
        self._count_spin.setEnabled(self._rb_count.isChecked())

    def value(self) -> str | None:
        freq_label = self._freq_combo.currentText()
        if freq_label == "None":
            return None
        freq = _FREQ_RRULE[freq_label]
        parts = [f"FREQ={freq}"]
        interval = self._interval_spin.value()
        if interval > 1:
            parts.append(f"INTERVAL={interval}")
        if freq == "WEEKLY":
            selected = [_BYDAY_CODES[i] for i, cb in enumerate(self._byday_checks) if cb.isChecked()]
            if selected:
                parts.append(f"BYDAY={','.join(selected)}")
        if self._rb_until.isChecked():
            qd = self._until_edit.date()
            parts.append(f"UNTIL={qd.year():04d}{qd.month():02d}{qd.day():02d}T000000Z")
        elif self._rb_count.isChecked():
            parts.append(f"COUNT={self._count_spin.value()}")
        return ";".join(parts)

    def set_value(self, rrule: str | None) -> None:
        if not rrule:
            self._freq_combo.setCurrentText("None")
            return
        props = _parse_rrule(rrule)
        freq_rrule = props.get("FREQ", "")
        freq_label = _RRULE_FREQ.get(freq_rrule, "None")
        self._freq_combo.setCurrentText(freq_label)
        interval = int(props.get("INTERVAL", "1"))
        self._interval_spin.setValue(interval)
        if "BYDAY" in props:
            codes = [c.strip() for c in props["BYDAY"].split(",")]
            for i, cb in enumerate(self._byday_checks):
                cb.setChecked(_BYDAY_CODES[i] in codes)
        if "UNTIL" in props:
            self._rb_until.setChecked(True)
            raw = props["UNTIL"].replace("Z", "").replace("T000000", "")
            try:
                d = date.fromisoformat(
                    f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw[:10]
                )
                from PySide6.QtCore import QDate
                self._until_edit.setDate(QDate(d.year, d.month, d.day))
            except Exception:
                pass
        elif "COUNT" in props:
            self._rb_count.setChecked(True)
            self._count_spin.setValue(int(props["COUNT"]))
        else:
            self._rb_never.setChecked(True)


def _parse_rrule(rrule: str) -> dict[str, str]:
    """Parse a flat RRULE string like FREQ=WEEKLY;BYDAY=MO,WE into a dict."""
    result: dict[str, str] = {}
    for part in rrule.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result
