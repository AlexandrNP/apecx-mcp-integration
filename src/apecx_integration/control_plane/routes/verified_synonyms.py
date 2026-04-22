"""Verified-synonym cache (T02 workflow-spec Steps 3a and 4p).

The VIOLIN × BV-BRC workflow (``docs/workflow_spec.md``) queries this
cache at Step 3a on every run. Cache hits short-circuit the LLM + HITL
review. The writeback endpoint (Step 4p) records a novel mapping after
an ``ApprovalStep`` (T10) approves it.

Design decisions:

- **Batched lookup**. A typical run extracts 5–50 entity terms; one
  HTTP call per term is 5–50× slower than necessary. ``POST
  /verified_synonyms/lookup`` takes a list and returns one
  ``VerifiedSynonymMatch`` per term. The response is ordered the same
  as the input.
- **Soft-delete only**. The ``VerifiedSynonym`` row is append-only in
  spirit — revocation flips ``is_active=false`` and records who / why.
  The unique constraint
  ``(source_vocabulary, query_term, target_vocabulary, scope, is_active)``
  allows the same tuple to exist both as an active row and as
  historical revoked rows; the create endpoint exploits this to
  guarantee at most one active mapping per tuple.
- **No pagination on lookup**. ``query_terms`` is capped at 500 items
  per request, enforced by the Pydantic schema. Heavier callers can
  batch-of-batches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apecx_integration.control_plane.dependencies import get_session
from apecx_integration.control_plane.models.entities import (
    VerifiedSynonym as VerifiedSynonymORM,
)
from apecx_integration.control_plane.schemas.api import (
    CreateVerifiedSynonymRequest,
    RevokeVerifiedSynonymRequest,
    VerifiedSynonymLookupRequest,
    VerifiedSynonymLookupResponse,
    VerifiedSynonymMatch,
    VerifiedSynonymResponse,
)
from apecx_integration.control_plane.schemas.entities import (
    VerifiedSynonym as VerifiedSynonymSchema,
)

router = APIRouter(prefix="/verified_synonyms", tags=["verified_synonyms"])


def _load_or_404(session: Session, synonym_id: UUID) -> VerifiedSynonymORM:
    row = session.get(VerifiedSynonymORM, synonym_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"verified_synonym {synonym_id} not found",
        )
    return row


@router.post("/lookup", response_model=VerifiedSynonymLookupResponse)
def lookup(
    body: VerifiedSynonymLookupRequest,
    session: Annotated[Session, Depends(get_session)],
) -> VerifiedSynonymLookupResponse:
    """Batched cache lookup. Returns one match per input term, in
    the same order as ``body.query_terms``. ``result`` is ``null`` for
    terms with no active mapping under the requested (source, target,
    scope) tuple.
    """
    stmt = (
        select(VerifiedSynonymORM)
        .where(VerifiedSynonymORM.source_vocabulary == body.source_vocabulary)
        .where(VerifiedSynonymORM.target_vocabulary == body.target_vocabulary)
        .where(VerifiedSynonymORM.query_term.in_(body.query_terms))
        .where(VerifiedSynonymORM.is_active.is_(True))
    )
    if body.scope is not None:
        stmt = stmt.where(VerifiedSynonymORM.scope == body.scope)
    else:
        stmt = stmt.where(VerifiedSynonymORM.scope.is_(None))

    rows = session.execute(stmt).scalars().all()
    by_term: dict[str, VerifiedSynonymORM] = {row.query_term: row for row in rows}

    matches = [
        VerifiedSynonymMatch(
            query_term=term,
            result=(
                VerifiedSynonymSchema.model_validate(by_term[term])
                if term in by_term
                else None
            ),
        )
        for term in body.query_terms
    ]
    return VerifiedSynonymLookupResponse(matches=matches)


@router.post("/", response_model=VerifiedSynonymResponse)
def create_verified_synonym(
    body: CreateVerifiedSynonymRequest,
    session: Annotated[Session, Depends(get_session)],
) -> VerifiedSynonymResponse:
    """Record a novel approved mapping. Called by the workflow's
    ``verified_synonym_writeback`` step after ``ApprovalStep`` returns
    an APPROVED / APPROVED_WITH_MODIFICATIONS decision.

    Enforces at most one active mapping per (source, query, target,
    scope) tuple via the unique index. A second write for the same
    tuple raises HTTP 409 — the caller is expected to either revoke
    the existing row first or treat its canonical_term as the
    already-recorded answer.
    """
    # App-level pre-check for uniqueness. The ORM carries a
    # ``UniqueConstraint`` over (source_vocabulary, query_term,
    # target_vocabulary, scope, is_active), but standard SQL treats
    # each NULL as distinct — so two active rows with scope=NULL
    # would NOT violate the DB constraint. We check explicitly.
    existing_stmt = (
        select(VerifiedSynonymORM)
        .where(VerifiedSynonymORM.source_vocabulary == body.source_vocabulary)
        .where(VerifiedSynonymORM.query_term == body.query_term)
        .where(VerifiedSynonymORM.target_vocabulary == body.target_vocabulary)
        .where(VerifiedSynonymORM.is_active.is_(True))
    )
    if body.scope is None:
        existing_stmt = existing_stmt.where(VerifiedSynonymORM.scope.is_(None))
    else:
        existing_stmt = existing_stmt.where(VerifiedSynonymORM.scope == body.scope)
    existing = session.execute(existing_stmt).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"active verified_synonym already exists for "
                f"(source={body.source_vocabulary!r}, "
                f"query={body.query_term!r}, "
                f"target={body.target_vocabulary!r}, "
                f"scope={body.scope!r}) — existing id={existing.id}. "
                "Revoke it first if you need to record a different mapping."
            ),
        )

    now = datetime.now(UTC)
    row = VerifiedSynonymORM(
        source_vocabulary=body.source_vocabulary,
        query_term=body.query_term,
        target_vocabulary=body.target_vocabulary,
        canonical_term=body.canonical_term,
        scope=body.scope,
        verified_by=body.verified_by,
        verified_at=now,
        confidence=body.confidence,
        source_run_id=body.source_run_id,
        comment=body.comment,
        is_active=True,
    )
    session.add(row)
    try:
        session.commit()
    except Exception as e:
        session.rollback()
        # The DB-level constraint still fires for non-null scopes; fall
        # back to 409 for those even though we checked above (race-safe).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"verified_synonym insert rejected by DB: {type(e).__name__}"
            ),
        ) from e
    session.refresh(row)
    return VerifiedSynonymResponse(verified_synonym=VerifiedSynonymSchema.model_validate(row))


@router.patch("/{synonym_id}", response_model=VerifiedSynonymResponse)
def revoke_verified_synonym(
    synonym_id: UUID,
    body: RevokeVerifiedSynonymRequest,
    session: Annotated[Session, Depends(get_session)],
) -> VerifiedSynonymResponse:
    """Soft-delete: set ``is_active=false``, record revocation metadata.

    The row is preserved for audit. ``superseded_by``, if set, points
    at the replacement row that the caller already created — giving
    the audit trail a forward pointer for "what did we change this
    to?" questions.

    Revoking a row that is already inactive is a 409 (idempotency is
    the caller's job; allowing double-revocation would overwrite the
    original revocation metadata).
    """
    row = _load_or_404(session, synonym_id)
    if not row.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"verified_synonym {synonym_id} is already inactive",
        )
    if body.superseded_by is not None:
        # Verify the replacement exists — otherwise we leave a dangling pointer.
        replacement = session.get(VerifiedSynonymORM, body.superseded_by)
        if replacement is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"superseded_by points at unknown id {body.superseded_by}",
            )
    row.is_active = False
    row.revoked_by = body.revoked_by
    row.revoked_at = datetime.now(UTC)
    row.revocation_reason = body.revocation_reason
    row.superseded_by = body.superseded_by
    session.commit()
    session.refresh(row)
    return VerifiedSynonymResponse(verified_synonym=VerifiedSynonymSchema.model_validate(row))
