"""Cluster AA — verified_synonyms PATCH revoke race.

The revoke route's idempotency check ("revoking an already-inactive
row is a 409") is implemented as ``read-then-conditional-write``:

    row = _load_or_404(session, synonym_id)
    if not row.is_active:
        raise 409                               # the idempotency gate
    row.is_active = False
    row.revoked_by = body.revoked_by            # ← per-call metadata
    row.revoked_at = datetime.now(UTC)
    row.revocation_reason = body.revocation_reason
    row.superseded_by = body.superseded_by
    session.commit()                            # UPDATE WHERE id=:id

Two concurrent PATCH calls with DIFFERENT revoked_by /
revocation_reason can both observe ``is_active == True`` (both
sessions read the same snapshot before either commits), both pass
the 409 gate, and both UPDATE the row. Last-writer-wins on the
revocation metadata: the first revoker's identity and reason are
silently overwritten.

That violates the explicit contract in the route's own docstring:

    "Revoking a row that is already inactive is a 409 (idempotency is
    the caller's job; allowing double-revocation would overwrite the
    original revocation metadata)."

Today the contract holds against sequential calls but breaks under
concurrent ones. The audit trail loses information.

Fix direction: conditional UPDATE.

    result = session.execute(
        update(VerifiedSynonym)
        .where(VerifiedSynonym.id == synonym_id)
        .where(VerifiedSynonym.is_active.is_(True))
        .values(is_active=False, revoked_by=..., ...)
    )
    session.commit()
    if result.rowcount == 0:
        raise 409  # somebody else already revoked us

Same shape as cluster V (approval race) and cluster Z (sweeper
race). The state-mutation pattern is the same; the cure is the
same.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.control_plane.app import create_app


pytestmark = pytest.mark.integration


async def test_concurrent_revoke_high_iteration_no_metadata_overwrite(
    cp_engine: Engine,
) -> None:
    """Run the race many times. We need a high-iteration probe
    because the natural race window between SELECT and UPDATE for
    each handler is microseconds — under SQLite's writer
    serialization the threads usually serialize cleanly. But
    "usually" is not "always."

    If even one iteration returns [200, 200], we have proof of the
    bug shape: two revokers passed the idempotency gate. After
    the run we cross-check that the persisted metadata matches
    the response identified as the winner.
    """
    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    ITERATIONS = 30
    statuses_seen: list[tuple[int, int]] = []
    metadata_mismatches: list[str] = []

    for i in range(ITERATIONS):
        # Fresh row per iteration so each race is independent.
        payload = {
            "source_vocabulary": "violin",
            "query_term": f"race-{i}",
            "target_vocabulary": "bvbrc",
            "canonical_term": f"Mapping {i}",
            "scope": f"scope-{i}",
            "verified_by": "alex",
            "confidence": 1.0,
        }
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            create_resp = await ac.post("/verified_synonyms/", json=payload)
        assert create_resp.status_code == 200, create_resp.text
        syn_id = UUID(create_resp.json()["verified_synonym"]["id"])

        body_a = {
            "revoked_by": "alice",
            "revocation_reason": f"alice-reason-{i}",
        }
        body_b = {
            "revoked_by": "bob",
            "revocation_reason": f"bob-reason-{i}",
        }

        async def _revoke(body):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                return await ac.patch(f"/verified_synonyms/{syn_id}", json=body)

        resp_a, resp_b = await asyncio.gather(_revoke(body_a), _revoke(body_b))
        statuses_seen.append((resp_a.status_code, resp_b.status_code))

        # If both succeeded, that is the bug shape — record what
        # ended up persisted vs what each call claimed.
        if resp_a.status_code == 200 and resp_b.status_code == 200:
            with cp_engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT revoked_by, revocation_reason FROM "
                        "verified_synonym WHERE id = :id"
                    ),
                    {"id": str(syn_id)},
                ).one()
            metadata_mismatches.append(
                f"iter={i} both 200; persisted=(revoked_by={row[0]!r}, "
                f"reason={row[1]!r}); alice_resp={resp_a.json()}; "
                f"bob_resp={resp_b.json()}"
            )

    n_double_ok = sum(1 for s in statuses_seen if s == (200, 200))
    n_one_one = sum(
        1 for s in statuses_seen if sorted(s) == [200, 409]
    )
    print(
        f"\n[revoke-race] iterations={ITERATIONS} "
        f"both-200={n_double_ok} one-200-one-409={n_one_one} "
        f"other={ITERATIONS - n_double_ok - n_one_one}"
    )
    if metadata_mismatches:
        print("[revoke-race] mismatches:")
        for m in metadata_mismatches:
            print(f"  {m}")

    assert n_double_ok == 0, (
        f"BUG: {n_double_ok}/{ITERATIONS} iterations had both PATCH calls "
        f"return 200. The route's read-then-check-then-update revoke "
        f"pattern is racy. Mismatches:\n"
        + "\n".join(f"  - {m}" for m in metadata_mismatches[:5])
        + "\nFix: conditional UPDATE WHERE is_active=TRUE; check rowcount."
    )


