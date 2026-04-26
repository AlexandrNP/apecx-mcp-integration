"""Cluster Y — verified_synonyms scope=NULL race probe.

The route's own comment admits the pre-check is racy for scope=NULL:

    # App-level pre-check for uniqueness. The ORM carries a
    # ``UniqueConstraint`` over (source_vocabulary, query_term,
    # target_vocabulary, scope, is_active), but standard SQL treats
    # each NULL as distinct — so two active rows with scope=NULL
    # would NOT violate the DB constraint. We check explicitly.

So:
- For scope=<non-null>: DB unique constraint catches the race.
- For scope=NULL: only the SELECT-then-INSERT pre-check guards. Racy.

Test: two concurrent POST /verified_synonyms with scope=NULL on
the same (source, query, target). Assert exactly one 200 + one
4xx, NOT two 200s.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.control_plane.app import create_app


pytestmark = pytest.mark.integration


async def test_verified_synonyms_concurrent_create_scope_null_no_dual_active(
    cp_engine: Engine,
) -> None:
    """The pre-check + commit pattern is racy for scope=NULL because
    SQL treats each NULL as distinct, so the unique constraint
    doesn't catch the race. Probe: fire two concurrent POSTs with
    the SAME (source, query, target) and scope=NULL. Assert only
    ONE row is active afterward.

    If both succeed, the cache has two contradictory active rows
    and the next /verified_synonyms/lookup returns whichever the
    DB happens to pick first — undefined.
    """
    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    payload = {
        "source_vocabulary": "violin",
        "query_term": "race-test-term",
        "target_vocabulary": "bvbrc",
        "canonical_term": "Race Test Mapping",
        "scope": None,
        "verified_by": "alex",
        "confidence": 1.0,
    }

    async def _create():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post("/verified_synonyms/", json=payload)

    resp_a, resp_b = await asyncio.gather(_create(), _create())

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    if statuses == [200, 200]:
        # Both succeeded. Count active rows for this (source, query,
        # target) tuple — if > 1, the bug is real.
        with cp_engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM verified_synonym "
                    "WHERE source_vocabulary = 'violin' "
                    "AND query_term = 'race-test-term' "
                    "AND target_vocabulary = 'bvbrc' "
                    "AND scope IS NULL "
                    "AND is_active = 1"
                )
            ).scalar_one()
        if count > 1:
            pytest.fail(
                f"verified_synonyms scope=NULL race produced {count} "
                f"active rows for the same (source, query, target). "
                "The pre-check failed to catch it (both transactions "
                "saw 0 existing rows; both committed). Fix direction: "
                "add a partial unique index on (source, query, target) "
                "WHERE scope IS NULL AND is_active=1, OR use a "
                "conditional INSERT WHERE NOT EXISTS."
            )

    # Acceptable shapes: [200, 4xx] from a successful pre-check race
    # win. Two 200s with only one row in DB is also acceptable
    # (very unlikely but technically idempotent if both INSERTed
    # the same row — won't happen because each gets a fresh UUID).
    assert statuses[0] == 200, (
        f"at least one create should succeed; got {statuses}"
    )
    assert statuses[1] in (200, 409, 422, 500), (
        f"second response should be 4xx/5xx (loser of race); got {statuses}"
    )

    # Final invariant: at most one active row.
    with cp_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM verified_synonym "
                "WHERE source_vocabulary = 'violin' "
                "AND query_term = 'race-test-term' "
                "AND target_vocabulary = 'bvbrc' "
                "AND scope IS NULL "
                "AND is_active = 1"
            )
        ).scalar_one()
    assert count == 1, (
        f"expected exactly 1 active row after race; got {count}. "
        "Two contradictory active rows means /verified_synonyms/lookup "
        "returns an undefined result for this term."
    )
