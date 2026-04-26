"""Adversarial async tests, round 2.

Cluster U found one real bug (orphan PENDING run on composer
failure). Cluster V continues pushing on suspected weak spots:

- V1: concurrent /approvals/approve + /approvals/reject on the same
  approval id — does the audit trail stay consistent? Two committed
  records with conflicting outcomes would be a real audit bug.

- V2: /hpc/ingest with corrupted provenance_seed.json — should be
  4xx (operator's fault), not 5xx (server's fault).

- V3: concurrent /workflows/execute on the same run — does the
  executor execute the workflow twice, or does state-transition
  serialization protect us?
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.schemas.enums import (
    ApprovalKind,
    ApprovalStatus,
)


pytestmark = pytest.mark.integration


def _seed_run_with_step_and_pending_approval(cp_engine: Engine):
    """Create a Run + a Step + a PENDING Approval. Returns the
    approval_id so tests can race on it.
    """
    run_id = uuid4()
    step_id = uuid4()
    approval_id = uuid4()
    now = datetime.now(UTC).isoformat()

    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PAUSED', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        # Step model: id, run_id, step_name, executor (NOT NULL),
        # status, ... (no summary, no step_id column). created_at
        # is NOT NULL since migration 0006 (cluster AH).
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) "
                "VALUES (:id, :rid, 'gate', 'LOCAL', 'PENDING', "
                "'[]', '[]', :ts)"
            ),
            {"id": str(step_id), "rid": str(run_id), "ts": now},
        )
        # Approval model: id, step_id, kind, status, policy,
        # created_at, ... (no summary). created_at is NOT NULL since
        # migration 0005 (cluster AE).
        conn.execute(
            text(
                "INSERT INTO approval (id, step_id, kind, status, "
                "policy, created_at) "
                "VALUES (:id, :sid, 'HARD', 'PENDING', '{}', :ts)"
            ),
            {
                "id": str(approval_id),
                "sid": str(step_id),
                "ts": now,
            },
        )
    return run_id, step_id, approval_id


# ---------------------------------------------------------------------------
# V1 — concurrent approve + reject on the same approval id
# ---------------------------------------------------------------------------


async def test_approval_concurrent_approve_and_reject_consistent_outcome(
    cp_engine: Engine,
) -> None:
    """Race: client A calls /approvals/approve, client B calls
    /approvals/reject, same approval_id, fired concurrently.

    Possible outcomes the architecture must enforce:
    - Exactly one of the two commits the new status; the other gets
      409 (already decided) once the loser re-reads.
    - The audit trail (provenance_event) has exactly one
      APPROVAL_DECIDED event for this approval. Two events with
      conflicting status would be a real audit bug.

    Pre-test suspicion: the route's ``_decide`` reads with each
    session's snapshot, calls ``_require_pending``, sets status,
    commits. SQLite WAL serializes the WRITES but each txn reads
    from its own snapshot — two concurrent reads can both see
    PENDING, both write a status, and we end up with TWO
    APPROVAL_DECIDED events.
    """
    _, _, approval_id = _seed_run_with_step_and_pending_approval(cp_engine)

    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def _approve():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/approvals/approve",
                json={
                    "approval_id": str(approval_id),
                    "comment": "alice",
                    "decided_by": "alice",
                },
            )

    async def _reject():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/approvals/reject",
                json={
                    "approval_id": str(approval_id),
                    "reason": "bob",
                    "decided_by": "bob",
                },
            )

    resp_a, resp_b = await asyncio.gather(_approve(), _reject())

    statuses = sorted([resp_a.status_code, resp_b.status_code])

    # Either: exactly one wins (200) and one loses (409 already-decided),
    # OR: both succeed and we have a real bug (audit-trail corruption).
    if statuses == [200, 200]:
        # Both routes claimed success. Check the audit trail.
        with cp_engine.connect() as conn:
            events = list(
                conn.execute(
                    text(
                        "SELECT payload FROM provenance_event "
                        "WHERE event_type = 'APPROVAL_DECIDED' "
                        "AND payload LIKE :pat"
                    ),
                    {"pat": f"%{approval_id}%"},
                )
            )

            final_status = conn.execute(
                text("SELECT status FROM approval WHERE id = :aid"),
                {"aid": str(approval_id)},
            ).scalar_one()

        decided_statuses = {
            json.loads(e[0])["status"] for e in events
        }
        pytest.fail(
            f"approval race produced TWO successful HTTP responses "
            f"({len(events)} APPROVAL_DECIDED events recorded with "
            f"statuses={decided_statuses}); final approval.status="
            f"{final_status}. The audit trail now claims this "
            f"approval was both APPROVED and REJECTED. Fix direction: "
            f"_decide should use a conditional UPDATE keyed on "
            f"status='PENDING' (mirror of the /hpc/ingest race fix)."
        )

    assert statuses == [200, 409], (
        f"expected one 200 + one 409 from concurrent approve+reject; "
        f"got {statuses}. responses: a={resp_a.text!r} b={resp_b.text!r}"
    )

    # Exactly one APPROVAL_DECIDED event for this approval.
    with cp_engine.connect() as conn:
        events = list(
            conn.execute(
                text(
                    "SELECT payload FROM provenance_event "
                    "WHERE event_type = 'APPROVAL_DECIDED' "
                    "AND payload LIKE :pat"
                ),
                {"pat": f"%{approval_id}%"},
            )
        )
    assert len(events) == 1, (
        f"expected exactly 1 APPROVAL_DECIDED event for this approval; "
        f"got {len(events)}. The race-loser path may be writing the "
        "provenance event before checking the conditional UPDATE rowcount."
    )


# ---------------------------------------------------------------------------
# V2 — /hpc/ingest with corrupt provenance_seed.json
# ---------------------------------------------------------------------------


async def test_hpc_ingest_corrupt_seed_json_returns_4xx_not_5xx(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """Operator-supplied bundle with a malformed provenance_seed.json
    must surface as a 4xx (client error: bundle is malformed),
    not a 5xx (server error). The two cases the operator should
    distinguish:

    - 422 with "provenance_seed.json" in the detail = the bundle
      is wrong, fix the bundle.
    - 5xx = something is broken on the Control Plane side; not the
      operator's problem.

    Three malformations to test:
    1. Invalid JSON syntax.
    2. Valid JSON but missing run_id.
    3. Valid JSON but run_id is not a UUID.
    """
    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def _ingest_with_seed(seed_text: str) -> httpx.Response:
        bundle = tmp_path / f"bundle_{uuid4()}"
        bundle.mkdir()
        (bundle / "provenance_seed.json").write_text(seed_text)
        # apecx_status.txt + outputs/result.json are required for a
        # complete bundle, but we expect to fail at seed parsing
        # before getting to those.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/hpc/ingest", json={"bundle_path": str(bundle)}
            )

    # 1. Invalid JSON syntax.
    r1 = await _ingest_with_seed("{ this is not valid json")
    assert 400 <= r1.status_code < 500, (
        f"corrupt JSON in seed should be 4xx; got {r1.status_code}: {r1.text}"
    )

    # 2. Valid JSON missing required keys.
    r2 = await _ingest_with_seed(json.dumps({"hello": "world"}))
    assert 400 <= r2.status_code < 500, (
        f"missing-fields seed should be 4xx; got {r2.status_code}: {r2.text}"
    )

    # 3. Valid JSON but run_id is not a UUID.
    r3 = await _ingest_with_seed(
        json.dumps(
            {
                "run_id": "not-a-uuid",
                "artifact_id": str(uuid4()),
                "library_version": "x",
                "llm_model": "y",
                "composition_summary_sentence": "z",
                "target_system": "polaris",
                "generated_at": "2026-04-25T00:00:00",
            }
        )
    )
    assert 400 <= r3.status_code < 500, (
        f"non-UUID run_id should be 4xx; got {r3.status_code}: {r3.text}"
    )


# ---------------------------------------------------------------------------
# V3 — concurrent /workflows/execute on the same run
# ---------------------------------------------------------------------------


async def test_concurrent_workflows_execute_same_run_serializes_or_409s(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """Two clients call /workflows/execute simultaneously on the
    same PAUSED run after a HITL approval. Possible outcomes:

    A. Both calls reach LocalExecutor.execute_run; both transition
       PAUSED -> RUNNING -> COMPLETED; the workflow runs TWICE.
       Real bug: side-effects of execution are non-idempotent.
    B. One wins (200 with terminal status), the other gets 409
       "run already terminal" or similar.
    C. Both 200, both reporting the same terminal state. Acceptable
       only if execute_run is fully idempotent — which the run-state
       audit trail wouldn't show.

    Outcome (A) is what we want to catch.

    Real-world risk: an MCP client that retries on a slow execute
    response could trigger this, doubling whatever side effects
    the workflow has.

    Setup: build a workflow that we can execute (real LocalExecutor
    against a stub composed-only YAML pointing at a no-op step),
    seed it as PAUSED, then race two execute calls.
    """
    from datetime import UTC, datetime
    from uuid import UUID
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.executors.local import LocalExecutor
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )

    REPO_ROOT = Path(__file__).resolve().parents[2]
    workflow_base = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "violin_bvbrc"
    )

    # Seed a Run + a workflow artifact that LocalExecutor can load.
    # We use the violin_bvbrc workflow YAML — it has real steps
    # (delimited file reader is the cheapest step) — but the test's
    # success criterion is about race-resolution, not the workflow
    # output, so we don't need to mock execution heavily.
    #
    # For this race test, simpler: write a 1-step yaml referencing
    # the real violin_bvbrc/steps/bvbrc_alphavirus_genomes_reader.yml
    # which loads instantly.
    workflow_yaml = textwrap.dedent(
        """\
        name: race_test_wf
        description: "1-step workflow used for race testing"
        version: "0.1.0"
        steps:
          read:
            class: "apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep"
            config: "steps/bvbrc_alphavirus_genomes_reader.yml"
        links: {}
        """
    )

    artifact_id = uuid4()
    on_disk = tmp_path / "wf.yml"
    on_disk.write_text(workflow_yaml)
    import hashlib
    h = hashlib.sha256(workflow_yaml.encode()).hexdigest()
    run_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()

    with cp_engine.begin() as conn:
        # Order matters: Run row first (no workflow_config_id FK
        # yet), then Artifact (FK to Run), then UPDATE Run to set
        # the back-link.
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "started_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts, :ts)"
            ),
            {"id": str(run_id), "ts": now_iso},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, :h, "
                ":sz, 'application/yaml', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "loc": str(on_disk),
                "h": h,
                "sz": len(workflow_yaml.encode()),
                "ts": now_iso,
            },
        )
        conn.execute(
            text(
                "UPDATE run SET workflow_config_id = :aid WHERE id = :rid"
            ),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=workflow_base,
    )

    app = create_app(engine=cp_engine, local_executor=executor)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def _execute() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/workflows/execute", json={"run_id": str(run_id)}, timeout=30.0
            )

    resp_a, resp_b = await asyncio.wait_for(
        asyncio.gather(_execute(), _execute()),
        timeout=60.0,
    )

    # Look for the bug: TWO RUN_STARTED events would mean execute
    # ran twice on the same run. ONE RUN_STARTED + 1 RUN_COMPLETED
    # is the safe shape (or 1 RUN_STARTED + 1 RUN_FAILED).
    with cp_engine.connect() as conn:
        started_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event "
                "WHERE run_id = :rid AND event_type = 'RUN_STARTED'"
            ),
            {"rid": str(run_id)},
        ).scalar_one()
        terminal_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event "
                "WHERE run_id = :rid "
                "AND event_type IN ('RUN_COMPLETED', 'RUN_FAILED')"
            ),
            {"rid": str(run_id)},
        ).scalar_one()

    if started_count > 1:
        pytest.fail(
            f"executor ran the workflow {started_count} times for the "
            f"same run_id. Two concurrent /workflows/execute calls "
            f"both transitioned the run from RUNNING through to "
            f"completion. Side effects of the workflow doubled. "
            f"Fix direction: LocalExecutor.execute_run should use a "
            f"conditional UPDATE on status -> EXECUTING (or "
            f"transition guard like the /hpc/ingest path)."
        )

    # If only one execute fired the workflow, both responses should
    # converge on the same terminal status (one ran, one observed
    # already-terminal).
    statuses = [resp_a.status_code, resp_b.status_code]
    assert all(s in (200, 409) for s in statuses), (
        f"expected 200 + (200 or 409); got {statuses}: "
        f"a={resp_a.text!r} b={resp_b.text!r}"
    )
    assert started_count == 1, (
        f"started_count = {started_count}, expected 1"
    )
    assert terminal_count == 1, (
        f"terminal_count = {terminal_count}, expected 1 (one "
        "RUN_COMPLETED or RUN_FAILED)"
    )
