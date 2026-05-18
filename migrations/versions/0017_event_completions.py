"""add event_completions table

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-18 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_completions",
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("uid", sa.String(), nullable=False),
        sa.Column("dtstart_utc", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("calendar_id", "uid", "dtstart_utc"),
    )
    op.create_index(
        "ix_event_completions_cal_dt",
        "event_completions",
        ["calendar_id", "dtstart_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_completions_cal_dt", table_name="event_completions")
    op.drop_table("event_completions")
