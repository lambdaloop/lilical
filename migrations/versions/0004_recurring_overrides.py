"""Recurring event overrides: add recurrence_id to event_instances; reset Graph data.

Phase 1 of recurring-events support switches Graph from storing pre-expanded
occurrences as plain rows to storing only the seriesMaster (with rrule) and
override/exception rows (with recurrence_id). The old occurrence rows stored
as individual events can't be distinguished from genuine single events, so we
clear ALL Graph calendar data and let the next sync re-fetch everything with
the corrected logic.

The event_instances table gains a recurrence_id column so that override
instances can be linked back to their specific EventRow (needed for
"edit this occurrence" in the UI).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add recurrence_id column to event_instances for override instance lookup.
    with op.batch_alter_table("event_instances") as batch_op:
        batch_op.add_column(
            sa.Column("recurrence_id", sa.Text, nullable=False, server_default="")
        )

    # Clear Graph calendar data so the next sync re-fetches with the new logic.
    op.execute("""
        DELETE FROM event_instances
        WHERE calendar_id IN (
            SELECT c.id FROM calendars c
            JOIN accounts a ON c.account_id = a.id
            WHERE a.kind = 'graph'
        )
    """)
    op.execute("""
        DELETE FROM events
        WHERE calendar_id IN (
            SELECT c.id FROM calendars c
            JOIN accounts a ON c.account_id = a.id
            WHERE a.kind = 'graph'
        )
    """)
    op.execute("""
        UPDATE calendars SET sync_cursor = NULL
        WHERE account_id IN (SELECT id FROM accounts WHERE kind = 'graph')
    """)


def downgrade() -> None:
    with op.batch_alter_table("event_instances") as batch_op:
        batch_op.drop_column("recurrence_id")
