"""unique RUN_STARTED per run — concurrent /workflows/execute guard

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-26

Found 2026-04-26 by adversarial concurrent-execute test (cluster V3):
two concurrent ``/workflows/execute`` calls on the same run both
transitioned the run from RUNNING through to COMPLETED, doubling
the workflow's side effects. The route's executor had no atomic
claim mechanism.

Fix: partial unique index ``uq_provenance_run_started_per_run`` on
``provenance_event(run_id) WHERE event_type='RUN_STARTED'``. The
``LocalExecutor`` records ``RUN_STARTED`` before doing any work;
the SECOND concurrent executor's record() will fail with
``IntegrityError`` on this index. The executor catches the error
and returns early without re-running the workflow.

This works on both SQLite (3.8+ supports partial indexes) and
Postgres (long-supported). The provenance recorder's
``threading.Lock`` already serializes record() calls within a
single process — combined with this DB-level guard, the
constraint holds across processes too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_provenance_run_started_per_run",
        "provenance_event",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("event_type = 'RUN_STARTED'"),
        postgresql_where=sa.text("event_type = 'RUN_STARTED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_provenance_run_started_per_run",
        table_name="provenance_event",
    )
