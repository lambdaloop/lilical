"""Create contacts and contacts_sync_state tables.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("inserted_at", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "source", "email", name="uq_contacts_account_source_email"),
    )
    op.create_index("idx_contacts_account_email", "contacts", ["account_id", "email"])
    op.create_index("idx_contacts_account_name", "contacts", ["account_id", "display_name"])

    op.create_table(
        "contacts_sync_state",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_full_refresh_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "source"),
    )


def downgrade() -> None:
    op.drop_table("contacts_sync_state")
    op.drop_index("idx_contacts_account_name", table_name="contacts")
    op.drop_index("idx_contacts_account_email", table_name="contacts")
    op.drop_table("contacts")
