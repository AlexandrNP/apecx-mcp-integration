"""step.created_at — give /runs/status a real ordering key for PENDING steps

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-26

Cluster AH (2026-04-26): the same UUID-tiebreak-as-ordering anti-pattern
as cluster AC (allocation_estimate) and AE (approval). The
``/runs/status`` endpoint orders steps by
``(started_at ASC NULLS LAST, id ASC)``. Steps that haven't started
yet have ``started_at = NULL``, so the secondary sort key is
``id`` — random uuid4. Multiple PENDING steps return in lex order,
not in the order they were defined / queued.

Today Step rows aren't authored by production code, so the bug
is latent. But ``/runs/status`` IS exercised, and any framework
change that starts writing Step rows would expose it
immediately. Fix preemptively while the table is empty: zero
backfill cost for existing rows (none exist).

Same migration shape as 0004 / 0005: add nullable, backfill
typed, batch_alter to NOT NULL.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backfill_ts = datetime.now(UTC)
    op.add_column(
        "step",
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE step SET created_at = :ts WHERE created_at IS NULL"
        ).bindparams(sa.bindparam("ts", backfill_ts, type_=sa.DateTime()))
    )
    with op.batch_alter_table("step") as batch_op:
        batch_op.alter_column("created_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("step") as batch_op:
        batch_op.drop_column("created_at")
