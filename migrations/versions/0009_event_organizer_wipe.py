"""Add organizer column and wipe all events so next sync re-populates with rich attendee/organizer data.

The attendees column shape changes from a JSON list of email strings to a JSON
list of dicts with {email, display_name, response, is_organizer, is_self}.
A new organizer column is added as a JSON dict {email, display_name, is_self}.
All event data is cleared so backends re-derive the richer shape on next sync.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the new organizer column.
    with op.batch_alter_table("events") as b:
        b.add_column(sa.Column("organizer", sa.Text(), nullable=True))

    # Wipe all event data across all backends so next sync rebuilds with the
    # richer attendee/organizer shape.
    op.execute("DELETE FROM pending_ops")
    op.execute("DELETE FROM event_instances")
    op.execute("DELETE FROM events")
    op.execute("UPDATE calendars SET sync_cursor = NULL")


def downgrade() -> None:
    with op.batch_alter_table("events") as b:
        b.drop_column("organizer")
