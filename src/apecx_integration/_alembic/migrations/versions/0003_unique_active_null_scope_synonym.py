"""unique active verified_synonym per (source, query, target) when scope IS NULL

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-26

The verified_synonym table carries a UniqueConstraint over
(source_vocabulary, query_term, target_vocabulary, scope, is_active).
But standard SQL treats each NULL as distinct, so two active rows
with scope=NULL and the same (source, query, target) do NOT violate
the constraint — and the create-route's app-level pre-check is racy
under concurrent POSTs.

Reproduced 2026-04-26 by adversarial test (cluster Y): two
concurrent /verified_synonyms/ POST calls with scope=NULL on the
same triple both return 200, leaving two active rows with
conflicting canonical_term values. /verified_synonyms/lookup then
returns an undefined result.

Fix: partial unique index over (source_vocabulary, query_term,
target_vocabulary) WHERE scope IS NULL AND is_active = 1. The
SECOND concurrent INSERT raises IntegrityError, which the route
already catches and re-raises as 409 (the existing fallback path
for non-NULL-scope rows).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_verified_synonym_active_null_scope",
        "verified_synonym",
        ["source_vocabulary", "query_term", "target_vocabulary"],
        unique=True,
        sqlite_where=sa.text("scope IS NULL AND is_active = 1"),
        postgresql_where=sa.text("scope IS NULL AND is_active = TRUE"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_verified_synonym_active_null_scope",
        table_name="verified_synonym",
    )
