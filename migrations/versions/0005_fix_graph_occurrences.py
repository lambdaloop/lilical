"""Clear Graph calendar data so next sync rebuilds with synthesized seriesMasters.

calendarView/delta only returns seriesMaster rows when the master's DTSTART
falls inside the calendar view window. Long-running recurring series (started
over a year ago) arrived as occurrence rows which the backend dropped, leaving
those series invisible. The backend now fetches missing masters via $batch and
injects them, but existing DB rows from the old sync are orphan occurrences
stored as singleInstance. Clearing Graph data lets the fixed initial_sync
rebuild the correct seriesMaster rows.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
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
    # Irreversible — we don't have the original event payloads.
    pass
