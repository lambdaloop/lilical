"""Reset sync cursor on Graph calendars again so organizer-owned series
attendees get their real responses fetched via /instances.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE calendars
        SET sync_cursor = NULL
        WHERE account_id IN (
            SELECT id FROM accounts WHERE kind = 'graph'
        )
        """
    )


def downgrade() -> None:
    pass
