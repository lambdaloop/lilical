from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from lilical.models.db import Base


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("calendar_id", "provider_event_id", name="uq_events_provider"),
        Index("idx_events_calendar", "calendar_id"),
        Index("idx_events_dirty", "local_dirty", sqlite_where=text("local_dirty=1")),
        Index(
            "idx_events_deleted",
            "deleted_locally",
            sqlite_where=text("deleted_locally=1"),
        ),
        Index(
            "idx_events_conflict",
            "conflict_state",
            sqlite_where=text("conflict_state IS NOT NULL"),
        ),
    )

    uid: Mapped[str] = mapped_column(String, primary_key=True)
    calendar_id: Mapped[str] = mapped_column(
        String, ForeignKey("calendars.id", ondelete="CASCADE"), primary_key=True
    )
    recurrence_id: Mapped[str] = mapped_column(String, primary_key=True, default="")
    provider_event_id: Mapped[str | None] = mapped_column(Text)
    dtstart: Mapped[str] = mapped_column(Text, nullable=False)
    dtend: Mapped[str] = mapped_column(Text, nullable=False)
    tz: Mapped[str] = mapped_column(Text, nullable=False)
    all_day: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    rrule: Mapped[str | None] = mapped_column(Text)
    exdates: Mapped[str | None] = mapped_column(Text)
    rdates: Mapped[str | None] = mapped_column(Text)
    attendees: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="CONFIRMED")
    # Current user's RSVP response for this event. One of:
    # "ACCEPTED", "TENTATIVE", "DECLINED", "NEEDS-ACTION", or NULL (not invited).
    self_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    transparency: Mapped[str] = mapped_column(Text, default="OPAQUE")
    valarms: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_modified: Mapped[str | None] = mapped_column(Text)
    local_dirty: Mapped[int] = mapped_column(Integer, default=0)
    deleted_locally: Mapped[int] = mapped_column(Integer, default=0)
    conflict_state: Mapped[str | None] = mapped_column(Text)
    local_modified_at: Mapped[str | None] = mapped_column(Text)
    inserted_at: Mapped[str] = mapped_column(Text)


class EventInstanceRow(Base):
    __tablename__ = "event_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String, nullable=False)
    calendar_id: Mapped[str] = mapped_column(String, nullable=False)
    dtstart_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    dtend_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    dtstart_local: Mapped[str] = mapped_column(Text, nullable=False)
    dtend_local: Mapped[str] = mapped_column(Text, nullable=False)
    all_day: Mapped[int] = mapped_column(Integer, default=0)
    is_override: Mapped[int] = mapped_column(Integer, default=0)


@dataclass(frozen=True, slots=True)
class Event:
    uid: str
    calendar_id: str
    provider_event_id: str | None = None
    dtstart: datetime | None = None
    dtend: datetime | None = None
    tz: str = "UTC"
    all_day: bool = False
    summary: str = ""
    description: str = ""
    location: str = ""
    url: str | None = None
    rrule: str | None = None
    recurrence_id: datetime | None = None
    exdates: tuple[datetime, ...] = ()
    rdates: tuple[datetime, ...] = ()
    attendees: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    color: str | None = None
    status: str = "CONFIRMED"
    # Current user's RSVP. One of ACCEPTED/TENTATIVE/DECLINED/NEEDS-ACTION
    # or None when the user isn't an invited attendee.
    self_response: str | None = None
    transparency: str = "OPAQUE"
    valarms: tuple[str, ...] = ()
    etag: str | None = None
    sequence: int = 0
    last_modified: datetime | None = None
    local_dirty: bool = False
    deleted_locally: bool = False
    conflict_state: str | None = None
