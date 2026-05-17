"""Reset sync cursor on Graph calendars so existing events refresh with
correct per-attendee response status (previously hard-coded to NEEDS-ACTION).

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
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
