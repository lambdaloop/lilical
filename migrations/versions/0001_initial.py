"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-13 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("server_url", sa.Text(), nullable=True),
        sa.Column("secret_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=True, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "calendars",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("is_visible", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("is_favorite", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("access_role", sa.Text(), nullable=True),
        sa.Column("sync_cursor", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("account_id", "provider_id", name="uq_calendars_provider"),
    )
    op.create_table(
        "events",
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("recurrence_id", sa.String(), nullable=False, server_default=""),
        sa.Column("provider_event_id", sa.Text(), nullable=True),
        sa.Column("dtstart", sa.Text(), nullable=False),
        sa.Column("dtend", sa.Text(), nullable=False),
        sa.Column("tz", sa.Text(), nullable=False),
        sa.Column("all_day", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=True, server_default=""),
        sa.Column("description", sa.Text(), nullable=True, server_default=""),
        sa.Column("location", sa.Text(), nullable=True, server_default=""),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("rrule", sa.Text(), nullable=True),
        sa.Column("exdates", sa.Text(), nullable=True),
        sa.Column("rdates", sa.Text(), nullable=True),
        sa.Column("attendees", sa.Text(), nullable=True),
        sa.Column("categories", sa.Text(), nullable=True),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True, server_default="CONFIRMED"),
        sa.Column("transparency", sa.Text(), nullable=True, server_default="OPAQUE"),
        sa.Column("valarms", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("local_dirty", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("deleted_locally", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("conflict_state", sa.Text(), nullable=True),
        sa.Column("local_modified_at", sa.Text(), nullable=True),
        sa.Column("inserted_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("uid", "calendar_id", "recurrence_id"),
        sa.ForeignKeyConstraint(["calendar_id"], ["calendars.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "calendar_id", "provider_event_id", name="uq_events_provider"
        ),
    )
    with op.batch_alter_table("events") as b:
        b.create_index("idx_events_calendar", ["calendar_id"])
        b.create_index(
            "idx_events_dirty", ["local_dirty"],
            sqlite_where=sa.text("local_dirty=1"),
        )
        b.create_index(
            "idx_events_deleted", ["deleted_locally"],
            sqlite_where=sa.text("deleted_locally=1"),
        )
        b.create_index(
            "idx_events_conflict", ["conflict_state"],
            sqlite_where=sa.text("conflict_state IS NOT NULL"),
        )
    op.create_table(
        "event_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("dtstart_utc", sa.Integer(), nullable=False),
        sa.Column("dtend_utc", sa.Integer(), nullable=False),
        sa.Column("dtstart_local", sa.Text(), nullable=False),
        sa.Column("dtend_local", sa.Text(), nullable=False),
        sa.Column("all_day", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("is_override", sa.Integer(), nullable=True, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("event_instances") as b:
        b.create_index("idx_instances_range", ["dtstart_utc", "dtend_utc"])
        b.create_index("idx_instances_calendar", ["calendar_id", "dtstart_utc"])
        b.create_index("idx_instances_uid", ["uid", "calendar_id"])
    op.create_table(
        "pending_ops",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("op", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("if_match", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("last_attempt_at", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["calendar_id"], ["calendars.id"]),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.execute("INSERT INTO settings(key,value) VALUES ('schema_version','0001')")


def downgrade() -> None:
    op.drop_table("pending_ops")
    op.drop_table("event_instances")
    op.drop_table("events")
    op.drop_table("calendars")
    op.drop_table("accounts")
    op.drop_table("settings")
