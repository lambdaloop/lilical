"""Chip-style invitee input with contact autocomplete."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from lilical.models.contact import Contact
    from lilical.models.event import Attendee
    from lilical.storage.contact_store import ContactStore


class _ContactModel(QAbstractListModel):
    def __init__(self, contact_store: "ContactStore", account_ids: list[str]) -> None:
        super().__init__()
        self._store = contact_store
        self._account_ids = account_ids
        self._results: list["Contact"] = []

    def refresh(self, prefix: str) -> None:
        self.beginResetModel()
        if prefix.strip():
            self._results = self._store.search(
                prefix, account_ids=self._account_ids or None, limit=20
            )
        else:
            self._results = []
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._results)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._results):
            return None
        c = self._results[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if c.display_name:
                return f"{c.display_name} <{c.email}>"
            return c.email
        if role == Qt.ItemDataRole.UserRole:
            return c
        return None


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

        label_text = attendee.display_name or attendee.email
        label = QLabel(label_text)
        label.setToolTip(attendee.email)
        layout.addWidget(label)

        btn = QPushButton("×")
        btn.setFixedSize(16, 16)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.ArrowCursor)
        btn.clicked.connect(lambda: self.removed.emit(self._attendee))
        layout.addWidget(btn)

    @property
    def attendee(self) -> "Attendee":
        return self._attendee


class _ChipLineEdit(QLineEdit):
    """QLineEdit that fires backspace_at_start when backspace is pressed at position 0."""

    backspace_at_start = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key.Key_Backspace
            and self.cursorPosition() == 0
            and not self.hasSelectedText()
        ):
            self.backspace_at_start.emit()
            return
        super().keyPressEvent(event)


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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        # Scroll area holding the chip flow.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setMaximumHeight(80)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._chip_container = QWidget()
        self._chip_layout = QHBoxLayout(self._chip_container)
        self._chip_layout.setContentsMargins(2, 2, 2, 2)
        self._chip_layout.setSpacing(4)
        self._chip_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._scroll.setWidget(self._chip_container)
        outer.addWidget(self._scroll)

        # Text input.
        self._edit = _ChipLineEdit()
        self._edit.setPlaceholderText("Add invitee…")
        self._edit.backspace_at_start.connect(self._on_backspace_at_start)
        self._edit.textChanged.connect(self._on_text_changed)
        self._edit.returnPressed.connect(self._commit_current)
        outer.addWidget(self._edit)

        # Completer.
        if contact_store is not None:
            self._model = _ContactModel(contact_store, self._account_ids)
            self._completer = QCompleter(self._model)
            self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self._completer.activated[QModelIndex].connect(self._on_completion_selected)
            self._edit.setCompleter(self._completer)
        else:
            self._model = None
            self._completer = None

        # Connect contacts_changed signal if available.
        if contact_store is not None:
            contact_store.contacts_changed.connect(lambda _: self._refresh_model())

    def _refresh_model(self) -> None:
        if self._model is not None:
            self._model.refresh(self._edit.text())

    def _on_text_changed(self, text: str) -> None:
        if self._model is not None:
            self._model.refresh(text)

    def _on_completion_selected(self, index: QModelIndex) -> None:
        contact = self._model.data(index, Qt.ItemDataRole.UserRole) if self._model else None
        if contact is not None:
            self._add_contact_chip(contact)
        else:
            self._commit_current()

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
        email = text.lower()
        if not self._email_already_added(email):
            from lilical.models.event import Attendee

            attendee = Attendee(email=email, response="NEEDS-ACTION")
            self._add_chip(attendee)
        self._edit.clear()

    def _email_already_added(self, email: str) -> bool:
        return any(c.attendee.email == email.lower() for c in self._chips)

    def _add_chip(self, attendee: "Attendee") -> None:
        chip = _InviteeChip(attendee, self._chip_container)
        chip.removed.connect(self._remove_chip)
        self._chip_layout.addWidget(chip)
        self._chips.append(chip)
        self.invitees_changed.emit()

    def _remove_chip(self, attendee: "Attendee") -> None:
        for chip in list(self._chips):
            if chip.attendee.email == attendee.email:
                self._chip_layout.removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
                self._chips.remove(chip)
                break
        self.invitees_changed.emit()

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

    def invitees(self) -> "list[Attendee]":
        return [c.attendee for c in self._chips]
