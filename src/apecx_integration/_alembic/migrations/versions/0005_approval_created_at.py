"""approval.created_at — give /approvals/pending a real ordering key

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-26

Same anti-pattern as cluster AC: ``/approvals/pending`` orders by
``ApprovalORM.id`` (random uuid4) so an operator polling the
backlog sees approvals in random order, not "oldest first." Add a
``created_at`` column, set it on insert, ORDER BY it.

Backfill: existing PENDING rows can't have a "real" creation
time, but they need SOME value to satisfy NOT NULL. The migration
timestamp is consistent across all backfilled rows; ``id`` is the
secondary tiebreak so old rows keep their pre-existing relative
order.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backfill_ts = datetime.now(UTC)
    op.add_column(
        "approval",
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    # Backfill with the migration timestamp. Same shape as
    # migration 0004; type the bindparam explicitly so Postgres
    # doesn't reject a VARCHAR going into a TIMESTAMP column
    # (cluster AC follow-up lesson).
    op.execute(
        sa.text(
            "UPDATE approval SET created_at = :ts WHERE created_at IS NULL"
        ).bindparams(sa.bindparam("ts", backfill_ts, type_=sa.DateTime()))
    )
    with op.batch_alter_table("approval") as batch_op:
        batch_op.alter_column("created_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("approval") as batch_op:
        batch_op.drop_column("created_at")
