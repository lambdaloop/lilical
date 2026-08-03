"""add recurrence_key identity and local_exdates tombstones

Two related fixes for per-occurrence recurring-event edits and deletions.

recurrence_key gives a recurrence slot an instant-based integer identity, so
the many ISO spellings of one slot (provider offset vs. UTC vs. the fixed-offset
value the UI computes) stop failing to match each other as strings.

local_exdates holds occurrences the user deleted locally until the server
confirms the hole. Previously that state lived only in the pending op, so the
moment the deletion uploaded, the next master upsert resurrected the occurrence.

event_instances is cleared so the materialized expansion is rebuilt with keys
populated; MainWindow rebuilds it on every launch.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-02 00:00:00.000000
"""
from datetime import datetime, time, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _key(recurrence_id: str, all_day: int) -> int:
    """Mirror of lilical.recurrence.identity.recurrence_key.

    Inlined deliberately: a migration must keep working if that module later
    changes shape.
    """
    if not recurrence_id:
        return 0
    try:
        dt = datetime.fromisoformat(recurrence_id)
    except ValueError:
        return 0
    if all_day:
        dt = datetime.combine(dt.date(), time.min, tzinfo=timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp() / 60.0)


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("recurrence_key", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("events", sa.Column("local_exdates", sa.Text(), nullable=True))
    op.add_column(
        "event_instances",
        sa.Column("recurrence_key", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "idx_events_rkey", "events", ["uid", "calendar_id", "recurrence_key"]
    )
    op.create_index(
        "idx_instances_rkey",
        "event_instances",
        ["uid", "calendar_id", "recurrence_key"],
    )

    # Backfill keys for existing override rows.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT uid, calendar_id, recurrence_id, all_day FROM events "
            "WHERE recurrence_id != ''"
        )
    ).fetchall()
    for uid, calendar_id, recurrence_id, all_day in rows:
        conn.execute(
            sa.text(
                "UPDATE events SET recurrence_key = :k WHERE uid = :u "
                "AND calendar_id = :c AND recurrence_id = :r"
            ),
            {
                "k": _key(recurrence_id, all_day),
                "u": uid,
                "c": calendar_id,
                "r": recurrence_id,
            },
        )

    # MainWindow runs rebuild_all_instances on every launch, so clearing the
    # materialized expansion is enough to repopulate recurrence_key. Sync
    # cursors are deliberately left alone — no re-download is needed.
    conn.execute(sa.text("DELETE FROM event_instances"))


def downgrade() -> None:
    op.drop_index("idx_instances_rkey", table_name="event_instances")
    op.drop_index("idx_events_rkey", table_name="events")
    op.drop_column("event_instances", "recurrence_key")
    op.drop_column("events", "local_exdates")
    op.drop_column("events", "recurrence_key")
