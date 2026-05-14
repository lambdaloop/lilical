from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lilical.models.db import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    server_url: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
