from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lilical.models.db import Base


class PendingOpRow(Base):
    __tablename__ = "pending_ops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id"), nullable=False
    )
    calendar_id: Mapped[str] = mapped_column(
        String, ForeignKey("calendars.id"), nullable=False
    )
    uid: Mapped[str] = mapped_column(String, nullable=False)
    op: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    if_match: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
