from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lilical.models.db import Base


class Calendar(Base):
    __tablename__ = "calendars"
    __table_args__ = (
        UniqueConstraint("account_id", "provider_id", name="uq_calendars_provider"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(Text)
    is_primary: Mapped[int] = mapped_column(Integer, default=0)
    is_visible: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_favorite: Mapped[int] = mapped_column(Integer, default=0)
    access_role: Mapped[str] = mapped_column(Text)
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[str | None] = mapped_column(Text)
