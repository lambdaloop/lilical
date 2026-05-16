"""Rename include_directory → include_contacts on accounts table.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.alter_column("include_directory", new_column_name="include_contacts")


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.alter_column("include_contacts", new_column_name="include_directory")
