"""Reset Graph sync_cursor again so organizer-owned series attendees are
refreshed via email-based organizer detection (fixes UPN/SMTP alias mismatch
that caused the Round 3 /instances fix to never fire).

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
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
