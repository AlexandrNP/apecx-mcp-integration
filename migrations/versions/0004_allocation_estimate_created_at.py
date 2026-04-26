"""allocation_estimate.created_at — give /hpc/confirm a real ordering key

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-26

The /hpc/confirm route picks the "latest" AllocationEstimate via
``ORDER BY id DESC LIMIT 1``. The id is a random uuid4 — lex-largest
has nothing to do with insertion order. Found 2026-04-26 by
adversarial probe (cluster AC): with two estimates whose UUIDs were
deliberately inverted relative to insertion time, /hpc/confirm
marked the OLDER row's user_confirmed=True and left the NEWER row
unconfirmed.

Fix: add a ``created_at`` timestamp column. The route will then
``ORDER BY created_at DESC, id DESC`` — chronological with id as
microsecond-tiebreak.

Backfill on upgrade: existing rows get the migration timestamp.
That's an arbitrary but consistent value; under tied timestamps
the route still falls back to ``id DESC`` (the old behavior) for
those grandfathered rows. New rows are correct from then on.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backfill_ts = datetime.now(UTC).isoformat()
    op.add_column(
        "allocation_estimate",
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    # Backfill existing rows so the NOT NULL change below doesn't
    # blow up. Same value for all of them; ``id`` continues to
    # tiebreak among them — fine because at this point no new
    # rows have shipped against the new column.
    op.execute(
        sa.text(
            "UPDATE allocation_estimate SET created_at = :ts "
            "WHERE created_at IS NULL"
        ).bindparams(ts=backfill_ts)
    )
    # SQLite supports ALTER COLUMN only via batch mode.
    with op.batch_alter_table("allocation_estimate") as batch_op:
        batch_op.alter_column("created_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("allocation_estimate") as batch_op:
        batch_op.drop_column("created_at")
