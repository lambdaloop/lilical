"""Clear Graph calendar data so next sync rebuilds with fixed exception handling.

Attendee-view exception events from Graph lack an `originalStart` field.
Previously these exceptions were stored with recurrence_id='' (empty string),
which collided with the seriesMaster row's primary key and overwrote it —
destroying the rrule and making the whole series invisible. The fix skips
such exceptions (returns None) so the master row is preserved intact.

Existing rows written under the old code are corrupt (rrule=None on what
should be seriesMaster rows, wrong dtstart). Clearing Graph data lets
initial_sync rebuild cleanly with the corrected logic.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM pending_ops
        WHERE calendar_id IN (
            SELECT c.id FROM calendars c
            JOIN accounts a ON c.account_id = a.id
            WHERE a.kind = 'graph'
        )
    """)
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
    pass
