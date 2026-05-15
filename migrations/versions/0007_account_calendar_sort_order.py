"""Add sort_order columns for drag-and-drop sidebar reordering.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as b:
        b.add_column(sa.Column("sort_order", sa.Integer(), nullable=True, server_default="0"))
    with op.batch_alter_table("calendars") as b:
        b.add_column(sa.Column("sort_order", sa.Integer(), nullable=True, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as b:
        b.drop_column("sort_order")
    with op.batch_alter_table("calendars") as b:
        b.drop_column("sort_order")
