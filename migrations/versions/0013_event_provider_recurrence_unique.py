"""Extend uq_events_provider to include recurrence_id.

CalDAV stores all VEVENTs from a single .ics resource (master + overrides)
with the same provider_event_id (the URL). The old two-column constraint
(calendar_id, provider_event_id) caused IntegrityError when a recurring event
with at least one override was synced in a single batch.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("uq_events_provider", type_="unique")
        batch.create_unique_constraint(
            "uq_events_provider",
            ["calendar_id", "provider_event_id", "recurrence_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("uq_events_provider", type_="unique")
        batch.create_unique_constraint(
            "uq_events_provider",
            ["calendar_id", "provider_event_id"],
        )
