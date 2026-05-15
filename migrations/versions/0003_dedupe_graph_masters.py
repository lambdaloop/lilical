"""Delete leftover seriesMaster EventRows from Graph calendars.

Microsoft Graph's calendarView/delta returns both seriesMaster events and
their pre-expanded occurrences. Before this fix, the seriesMaster was stored
as an EventRow with rrule set, and _rebuild_instances_for expanded it into
duplicate event_instances alongside the N per-occurrence instances already
produced — doubling every recurring event. The seriesMaster rows are now
dropped at the backend boundary; this migration removes any that were already
written. The orphaned event_instances are cleared by rebuild_all_instances on
next startup.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM events
        WHERE rrule IS NOT NULL
          AND calendar_id IN (
            SELECT c.id FROM calendars c
            JOIN accounts a ON c.account_id = a.id
            WHERE a.kind = 'graph'
          )
    """)


def downgrade() -> None:
    # Irreversible — we don't have the original master payloads.
    pass
