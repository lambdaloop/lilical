from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lilical.models.db import Base


@dataclass(frozen=True, slots=True)
class Contact:
    email: str
    display_name: str | None
    source: str  # "directory" | "personal" | "other" | "harvested"
    account_id: str
    source_id: str | None = None
    last_seen_at: str | None = None


# Priority ordering for contacts (lower = higher priority).
SOURCE_PRIORITY: dict[str, int] = {
    "directory": 0,
    "personal": 1,
    "other": 2,
    "harvested": 3,
}


class ContactRow(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "source", "email", name="uq_contacts_account_source_email"
        ),
        Index("idx_contacts_account_email", "account_id", "email"),
        Index("idx_contacts_account_name", "account_id", "display_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inserted_at: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ContactsSyncStateRow(Base):
    __tablename__ = "contacts_sync_state"

    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String, primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_full_refresh_at: Mapped[str | None] = mapped_column(Text)
