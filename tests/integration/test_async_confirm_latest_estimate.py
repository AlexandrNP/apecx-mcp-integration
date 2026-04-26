"""Cluster AC — /hpc/confirm picks the wrong AllocationEstimate.

The route uses

    SELECT * FROM allocation_estimate
    WHERE run_id = :run_id
    ORDER BY id DESC LIMIT 1

to find "the latest" estimate to confirm. ``id`` is a random
``uuid4()`` — NOT a monotonically increasing integer and NOT a
timestamp. The lex-largest UUID has nothing to do with insertion
order. So when the user calls ``/hpc/estimate`` twice (e.g.,
they wanted to see what changed after editing the workflow), and
then ``/hpc/confirm``, the confirm route picks whichever of the
two rows happens to have the lex-larger UUID — chance, not
chronology.

Symptoms:
- The user's confirmation lands on the OLDER estimate roughly
  half the time. The newer estimate stays ``user_confirmed=False``.
- If the workflow grew between estimates, the user thinks they
  approved the new (larger) allocation but actually only signed
  off on the old (smaller) one.
- The audit trail shows ``user_confirmed_at`` on the wrong row.

Same anti-pattern as cluster X (provenance chain "latest" via
``timestamp DESC, id DESC`` where the id-tiebreaker on tied
timestamps is a random UUID; cluster X fixed that with a
process-local cursor cache). The hpc table has it WORSE because
there's no timestamp at all — ``id DESC`` is the SOLE order key.

Fix: AllocationEstimate needs a monotonic ordering key. Two
options:

  A) Add ``created_at: datetime`` (default=now) and order by it
     descending. A second-level tiebreaker on ``id`` is fine
     because identical-microsecond rows are extremely unusual on
     a single-process run, and a deterministic-but-wrong tiebreak
     on a tie is much smaller than a deterministic-but-wrong
     primary order on a non-tie.
  B) Replace the random-UUID primary key with an
     auto-increment integer ``id``. Bigger schema churn.

Option A is the smaller migration and matches the rest of the
schema (``Run.created_at``, ``Artifact.created_at`` exist). This
test asserts the contract: ``/hpc/confirm`` must mark the
**most recently inserted** estimate, regardless of UUID lex
order.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run_with_workflow(cp_engine: Engine) -> UUID:
    """Insert a Run with a satisfied workflow_config_id so the
    /hpc/estimate route doesn't 404 on us. We don't actually load
    the YAML here — we bypass /hpc/estimate and write
    AllocationEstimate rows directly to the table, controlling
    UUIDs and insertion order.
    """
    from uuid import uuid4
    from datetime import UTC, datetime

    run_id = uuid4()
    artifact_id = uuid4()
    now = datetime.now(UTC).isoformat()

    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', '/tmp/none', "
                "'sha256-placeholder', 1, 'application/x-yaml', :ts)"
            ),
            {"id": str(artifact_id), "rid": str(run_id), "ts": now},
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return run_id


def _insert_estimate(
    cp_engine: Engine,
    *,
    run_id: UUID,
    estimate_id: UUID,
    estimated_core_hours: float,
) -> None:
    """Insert with raw SQL so the test controls the UUID directly.
    Each call uses ``datetime.now`` for ``created_at``; consecutive
    calls land at strictly increasing microseconds so the route's
    ORDER BY created_at DESC has a deterministic answer.
    """
    from datetime import UTC, datetime
    import time

    # Sleep a microsecond so two back-to-back calls have distinct
    # created_at — SQLite's DateTime resolution is good enough for
    # microseconds, and the test's "older vs newer" semantics rely
    # on the timestamps being distinguishable.
    time.sleep(0.001)
    # Use the ORM so the stored ``created_at`` format matches what
    # the production /hpc/estimate route writes. Earlier helpers
    # used ``datetime.isoformat()`` strings (T-separator + tz) which
    # mismatch the ORM's space-separator-no-tz format. Mixing formats
    # in one column breaks lex comparison and thus cluster AK's
    # race-detection predicate. Cluster AK follow-up (2026-04-26).
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.models.entities import (
        AllocationEstimate as AEORM,
    )

    factory = make_session_factory(cp_engine)
    with factory() as session:
        session.add(
            AEORM(
                id=estimate_id,
                run_id=run_id,
                estimated_core_hours=estimated_core_hours,
                estimated_wall_time_seconds=estimated_core_hours * 3600.0,
                endpoint="polaris",
                user_confirmed=False,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


def test_confirm_picks_most_recently_inserted_not_lex_largest_uuid(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Insert two AllocationEstimate rows with deliberately
    inverted UUID ordering: the first-inserted gets a lex-LARGE
    UUID, the second-inserted gets a lex-SMALL UUID.

    Then /hpc/confirm. The contract under any sane design is "I
    confirm the latest estimate." The current ``ORDER BY id DESC``
    implementation will pick the lex-largest, which is the
    EARLIER row. The newer row never gets ``user_confirmed=True``.
    """
    run_id = _seed_run_with_workflow(cp_engine)

    # OLDER row, but with lex-LARGE UUID.
    older_id = UUID("ffffffff-ffff-4fff-bfff-ffffffffff01")
    _insert_estimate(
        cp_engine, run_id=run_id, estimate_id=older_id, estimated_core_hours=10.0
    )

    # NEWER row, but with lex-SMALL UUID.
    newer_id = UUID("00000000-0000-4000-8000-000000000001")
    _insert_estimate(
        cp_engine, run_id=run_id, estimate_id=newer_id, estimated_core_hours=20.0
    )

    # Confirm with confirmed_core_hours sufficient for BOTH the
    # older (10) and newer (20) estimate; if this gets routed to
    # the older row, we'd never know from the response code alone.
    resp = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": 25.0},
    )
    assert resp.status_code == 200, resp.text

    # Inspect both rows. Whichever is marked user_confirmed=True
    # tells us what /hpc/confirm interpreted as "latest."
    with cp_engine.connect() as conn:
        older_confirmed = conn.execute(
            text(
                "SELECT user_confirmed FROM allocation_estimate "
                "WHERE id = :id"
            ),
            {"id": str(older_id)},
        ).scalar_one()
        newer_confirmed = conn.execute(
            text(
                "SELECT user_confirmed FROM allocation_estimate "
                "WHERE id = :id"
            ),
            {"id": str(newer_id)},
        ).scalar_one()

    print(
        f"\n[confirm-latest] older_confirmed={older_confirmed} "
        f"newer_confirmed={newer_confirmed}"
    )

    # The bug: confirm picks the lex-largest UUID, which is the
    # OLDER row. So older_confirmed==1 and newer_confirmed==0.
    assert newer_confirmed in (1, True), (
        f"BUG: /hpc/confirm marked user_confirmed=True on the OLDER "
        f"estimate (uuid={older_id}, lex-large) instead of the NEWER "
        f"estimate (uuid={newer_id}, lex-small). The route uses "
        "ORDER BY id DESC where id is a random uuid4 — that picks "
        "the lex-largest UUID, which has nothing to do with chronology. "
        "Fix: add a created_at column on AllocationEstimate and "
        "order by it; tiebreak on id."
    )
    assert older_confirmed in (0, False), (
        f"both estimates ended up confirmed (older={older_confirmed}, "
        f"newer={newer_confirmed}); confirm should affect exactly one row"
    )


def test_confirm_picks_correctly_when_uuid_order_matches_insertion_order(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Positive control: when the lex-larger UUID happens to also
    be the most-recently inserted, the route works. This is the
    50% of cases where the random ordering "luckily" agrees with
    chronology — confirms that the bug shape is purely about UUID
    lex-order vs insertion order.
    """
    run_id = _seed_run_with_workflow(cp_engine)

    older_id = UUID("00000000-0000-4000-8000-000000000001")
    _insert_estimate(
        cp_engine, run_id=run_id, estimate_id=older_id, estimated_core_hours=10.0
    )

    newer_id = UUID("ffffffff-ffff-4fff-bfff-ffffffffff01")
    _insert_estimate(
        cp_engine, run_id=run_id, estimate_id=newer_id, estimated_core_hours=20.0
    )

    resp = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": 25.0},
    )
    assert resp.status_code == 200, resp.text

    with cp_engine.connect() as conn:
        newer_confirmed = conn.execute(
            text(
                "SELECT user_confirmed FROM allocation_estimate "
                "WHERE id = :id"
            ),
            {"id": str(newer_id)},
        ).scalar_one()

    assert newer_confirmed in (1, True), (
        "lucky-ordering case should confirm the newer row"
    )
