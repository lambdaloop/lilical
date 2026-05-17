"""Chip-style invitee input with contact autocomplete popup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lilical.ui.widgets.flow_layout import FlowLayout
from lilical.utils.names import format_display_name

if TYPE_CHECKING:
    from lilical.models.contact import Contact
    from lilical.models.event import Attendee
    from lilical.storage.contact_store import ContactStore


class _CompletionPopup(QListWidget):
    """Autocomplete popup backed by QListWidget + Qt::Popup window flag.

    Uses the same window machinery as QComboBox's dropdown (Qt::Popup +
    WA_ShowWithoutActivating), bypassing QCompleter's fragile interaction
    with beginResetModel/endResetModel that caused input events to never
    reach the popup view.
    """

    contact_selected = Signal(object)  # Contact

    def __init__(self, anchor: QLineEdit) -> None:
        super().__init__()
        self._anchor = anchor
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.itemClicked.connect(self._on_item_clicked)
        self.hide()

    def show_results(self, results: "list[Contact]") -> None:
        self.clear()
        for c in results:
            display = format_display_name(c.display_name)
            label = f"{display} <{c.email}>" if display else c.email
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, c)
            self.addItem(item)
        if not self.count():
            self.hide()
            return
        bl = self._anchor.mapToGlobal(self._anchor.rect().bottomLeft())
        self.move(bl)
        self.setFixedWidth(self._anchor.width())
        row_h = max(24, self.sizeHintForRow(0))
        self.setFixedHeight(min(8, self.count()) * row_h + 4)
        self.setCurrentRow(0)
        self.show()

    def navigate(self, delta: int) -> None:
        if self.count():
            self.setCurrentRow(max(0, min(self.count() - 1, self.currentRow() + delta)))

    def commit_current(self) -> None:
        item = self.currentItem()
        if item is not None:
            self._on_item_clicked(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        c: "Contact | None" = item.data(Qt.ItemDataRole.UserRole)
        if c is not None:
            self.contact_selected.emit(c)
        self.hide()


class _InviteeChip(QFrame):
    removed = Signal(object)  # emits the Attendee

    def __init__(self, attendee: "Attendee", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._attendee = attendee
        self.setObjectName("inviteeChip")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 2, 2)
        layout.setSpacing(4)

        label_text = format_display_name(attendee.display_name) or attendee.email
        label = QLabel(label_text)
        label.setToolTip(attendee.email)
        layout.addWidget(label)

        btn = QToolButton()
        btn.setText("×")
        btn.setObjectName("inviteeChipRemove")
        btn.setCursor(Qt.CursorShape.ArrowCursor)
        btn.clicked.connect(lambda: self.removed.emit(self._attendee))
        layout.addWidget(btn)

    @property
    def attendee(self) -> "Attendee":
        return self._attendee


class _ChipLineEdit(QLineEdit):
    """QLineEdit that forwards popup navigation keys and fires backspace_at_start."""

    backspace_at_start = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        parent = self.parent()
        popup: "_CompletionPopup | None" = getattr(parent, "_popup", None) if parent is not None else None
        if popup is not None and popup.isVisible():
            key = event.key()
            if key == Qt.Key.Key_Down:
                popup.navigate(+1)
                return
            if key == Qt.Key.Key_Up:
                popup.navigate(-1)
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                popup.commit_current()
                return
            if key == Qt.Key.Key_Escape:
                popup.hide()
                return
        if (
            event.key() == Qt.Key.Key_Backspace
            and self.cursorPosition() == 0
            and not self.hasSelectedText()
        ):
            self.backspace_at_start.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        parent = self.parent()
        popup: "_CompletionPopup | None" = getattr(parent, "_popup", None) if parent is not None else None
        if popup is not None:
            # Defer so a popup click still registers before the popup hides.
            QTimer.singleShot(150, popup.hide)


class InviteeChipEdit(QWidget):
    """Chip-style multi-invitee input with contact autocomplete popup."""

    invitees_changed = Signal()

    def __init__(
        self,
        contact_store: "ContactStore | None" = None,
        account_ids: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._contact_store = contact_store
        self._account_ids = account_ids or []
        self._chips: list[_InviteeChip] = []
        self._pending_query = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._run_search)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        # Text input (on top).
        self._edit = _ChipLineEdit()
        self._edit.setPlaceholderText("Add invitee…")
        self._edit.backspace_at_start.connect(self._on_backspace_at_start)
        self._edit.textChanged.connect(self._on_text_changed)
        self._edit.returnPressed.connect(self._commit_current)
        outer.addWidget(self._edit)

        # Scroll area holding the chip flow (below input, hidden when empty).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setMaximumHeight(120)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._scroll.setVisible(False)

        self._chip_container = QWidget()
        self._chip_layout = FlowLayout(self._chip_container, margin=2, h_spacing=4, v_spacing=4)
        self._scroll.setWidget(self._chip_container)
        outer.addWidget(self._scroll)

        if contact_store is not None:
            self._popup = _CompletionPopup(self._edit)
            self._popup.contact_selected.connect(self._add_contact_chip)
        else:
            self._popup = None

    def _on_text_changed(self, text: str) -> None:
        if self._popup is None:
            return
        self._pending_query = text
        if not text.strip():
            self._search_timer.stop()
            self._popup.hide()
            return
        self._search_timer.start()

    def _run_search(self) -> None:
        if self._contact_store is None or self._popup is None:
            return
        if not self._pending_query.strip():
            self._popup.hide()
            return
        results = self._contact_store.search(
            self._pending_query, account_ids=self._account_ids or None, limit=20
        )
        if results:
            self._popup.show_results(results)
        else:
            self._popup.hide()

    def _add_contact_chip(self, contact: "Contact") -> None:
        from lilical.models.event import Attendee

        email = contact.email
        if self._email_already_added(email):
            self._edit.clear()
            return
        attendee = Attendee(
            email=email,
            display_name=contact.display_name,
            response="NEEDS-ACTION",
        )
        self._add_chip(attendee)
        self._edit.clear()

    def _commit_current(self) -> None:
        text = self._edit.text().strip().rstrip(",").strip()
        if not text:
            return
        if "<" in text and text.endswith(">"):
            email = text[text.rindex("<") + 1 : -1].strip().lower()
        else:
            email = text.lower()
        if "@" not in email:
            self._edit.clear()
            return
        if not self._email_already_added(email):
            from lilical.models.event import Attendee

            self._add_chip(Attendee(email=email, response="NEEDS-ACTION"))
        self._edit.clear()

    def _email_already_added(self, email: str) -> bool:
        return any(c.attendee.email == email.lower() for c in self._chips)

    def _add_chip(self, attendee: "Attendee") -> None:
        chip = _InviteeChip(attendee, self._chip_container)
        chip.removed.connect(self._remove_chip)
        self._chip_layout.addWidget(chip)
        self._chips.append(chip)
        self._update_scroll_visibility()
        self.invitees_changed.emit()

    def _remove_chip(self, attendee: "Attendee") -> None:
        for chip in list(self._chips):
            if chip.attendee.email == attendee.email:
                self._chip_layout.removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
                self._chips.remove(chip)
                break
        self._update_scroll_visibility()
        self.invitees_changed.emit()

    def _update_scroll_visibility(self) -> None:
        self._scroll.setVisible(bool(self._chips))

    def _on_backspace_at_start(self) -> None:
        if self._chips:
            last = self._chips[-1]
            self._remove_chip(last.attendee)

    def set_invitees(self, attendees: "list[Attendee] | tuple[Attendee, ...]") -> None:
        for chip in list(self._chips):
            self._chip_layout.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        for a in attendees:
            if not a.is_self:
                self._add_chip(a)
        self._update_scroll_visibility()

    def invitees(self) -> "list[Attendee]":
        return [c.attendee for c in self._chips]
