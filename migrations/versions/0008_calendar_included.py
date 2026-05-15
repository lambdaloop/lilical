"""Add is_included to calendars for workspace inclusion vs. per-session visibility.

Existing rows with is_visible=0 were hidden via Choose Calendars (the chip-toggle
is new), so we migrate their intent: mark them excluded and reset is_visible to 1
so the chip starts from a clean "shown" state.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("calendars") as b:
        b.add_column(sa.Column("is_included", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE calendars SET is_included = 0, is_visible = 1 WHERE is_visible = 0")


def downgrade() -> None:
    with op.batch_alter_table("calendars") as b:
        b.drop_column("is_included")
