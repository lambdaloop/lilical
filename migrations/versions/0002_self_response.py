"""add self_response to events

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("events") as b:
        b.add_column(sa.Column("self_response", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events") as b:
        b.drop_column("self_response")
