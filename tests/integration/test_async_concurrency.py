"""Async concurrency regression guards.

Three clusters of fixes shipped during the audit pass involve async
lifecycle but landed without real concurrency tests. Audit notes
flagged race-loser tests as "deadlock-prone" and skipped them. With
the real architecture in place and the bugs found via cluster O's
e2e probe pattern, the right move is now to write the concurrency
tests that should have shipped earlier.

Tests below use ``httpx.AsyncClient`` over ``httpx.ASGITransport`` so
``asyncio.gather`` actually issues concurrent in-process requests
against the same FastAPI app — not threads pretending to be
concurrent. Each test maps to a specific audit finding:

- ``test_concurrent_hpc_ingest_one_winner_one_409``
    Audit §2.3 — pre-fix two ingests on the same run could both pass
    the read-time terminal check and write conflicting outcomes.
    Cluster E shipped a conditional UPDATE; this test proves it
    holds under real concurrent dispatch.

- ``test_concurrent_workflows_start_no_session_pool_starvation``
    Audit §2.1 — pre-fix the route held a SQLAlchemy session across
    ``await composer.compose(...)``. Cluster F restructured to
    release the session before the await. Test issues 5 concurrent
    /workflows/start calls (each composer call ~30s on warm Ollama)
    and asserts none time out, all succeed, and Run rows are
    distinct.

- ``test_provenance_recorder_concurrent_appends_no_chain_break``
    Audit §2.5 — ProvenanceRecorder uses ``threading.Lock`` which is
    sync-blocking. Under async load, two coroutines on the same
    event loop can both acquire/release without losing chain
    integrity (sync lock effectively serializes). This test
    verifies the chain stays intact under concurrent dispatch.
    The test does NOT prove freedom from event-loop-blocking
    pauses; that's a perf concern, not a correctness one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType


pytestmark = pytest.mark.integration


SAMPLE_YAML = "name: wf\nsteps: {}\nlinks: {}\n"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_run_with_workflow_artifact(engine: Engine, on_disk: Path):
    """Insert a Run + GENERATED_WORKFLOW Artifact in non-terminal state.

    Mirrors ``test_api_hpc_ingest._insert_run_with_artifact`` but lives
    in this file so the test isn't load-bearing on a sibling import
    path.
    """
    run_id = uuid4()
    artifact_id = uuid4()
    on_disk.write_text(SAMPLE_YAML)
    h = hashlib.sha256(SAMPLE_YAML.encode()).hexdigest()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, started_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts, :ts)"
            ),
            {"id": str(run_id), "ts": now},
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
                "sz": len(SAMPLE_YAML.encode()),
                "ts": now,
            },
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return run_id, artifact_id


def _make_completed_bundle(
    bundle_dir: Path,
    run_id: UUID,
    artifact_id: UUID,
    *,
    rows: int,
    status_text: str = "completed",
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "provenance_seed.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "artifact_id": str(artifact_id),
                "library_version": "0.1.0-test",
                "llm_model": "mistral-nemo:latest",
                "composition_summary_sentence": "concurrency test",
                "target_system": "polaris",
                "generated_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    (bundle_dir / "apecx_status.txt").write_text(status_text)
    (bundle_dir / "outputs").mkdir(exist_ok=True)
    (bundle_dir / "outputs" / "result.json").write_text(
        json.dumps({"status": "ok", "rows": rows})
    )
    return bundle_dir


def _count_output_artifacts(engine: Engine, run_id: UUID) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM artifact "
                "WHERE run_id = :rid AND kind = 'OUTPUT'"
            ),
            {"rid": str(run_id)},
        ).first()
    return int(row[0])


# ---------------------------------------------------------------------------
# Cluster P — concurrent /hpc/ingest
# ---------------------------------------------------------------------------


async def test_concurrent_hpc_ingest_one_winner_one_409(
    cp_engine: Engine, tmp_path: Path
) -> None:
    """Audit §2.3 (cluster E) regression guard.

    Two clients race on /hpc/ingest for the same run. Pre-fix, both
    could pass the read-time terminal check and both could write
    OUTPUT artifacts plus terminal-status updates, leaving the DB
    in a wedged state.

    Post-fix (cluster E), the conditional ``UPDATE ... WHERE status
    NOT IN (terminal)`` is atomic at the SQL layer — only one
    update can match the non-terminal row. The loser sees rowcount=0,
    rolls back its OUTPUT-artifact write, and the route returns 409
    with "concurrent ingest" in the detail.

    This test sets up two distinct bundles for the same run, fires
    them concurrently via ``asyncio.gather``, and asserts the
    one-winner / one-409 contract holds.
    """
    run_id, artifact_id = _seed_run_with_workflow_artifact(
        cp_engine, tmp_path / "wf.yml"
    )
    bundle_a = _make_completed_bundle(
        tmp_path / "bundle_a", run_id, artifact_id, rows=1,
    )
    bundle_b = _make_completed_bundle(
        tmp_path / "bundle_b", run_id, artifact_id, rows=2,
    )

    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app)

    async def _ingest(bundle: Path) -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/hpc/ingest", json={"bundle_path": str(bundle)}
            )

    # Fire both concurrently. With SQLite WAL the writes serialize at
    # the WAL writer; with Postgres they serialize on the row lock.
    # In either case exactly one should win.
    resp_a, resp_b = await asyncio.gather(
        _ingest(bundle_a), _ingest(bundle_b)
    )

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    assert statuses == [200, 409], (
        f"expected one 200 + one 409 from concurrent ingest; got "
        f"{statuses!r}. responses: a={resp_a.text!r} b={resp_b.text!r}"
    )

    loser = resp_a if resp_a.status_code == 409 else resp_b
    assert "concurrent" in loser.json()["detail"].lower() or (
        "terminal state" in loser.json()["detail"].lower()
    ), (
        f"loser's 409 detail must name the concurrent-ingest case; "
        f"got: {loser.json()['detail']!r}"
    )

    # Run is in a terminal state, exactly one OUTPUT artifact persists.
    with cp_engine.connect() as conn:
        run_status = conn.execute(
            text("SELECT status FROM run WHERE id = :rid"),
            {"rid": str(run_id)},
        ).scalar_one().lower()
    assert run_status == "completed", (
        f"after concurrent ingest, run.status should be 'completed' "
        f"(the winning ingest's outcome); got {run_status!r}."
    )
    assert _count_output_artifacts(cp_engine, run_id) == 1, (
        "the loser's OUTPUT-artifact write should have been rolled "
        "back when its conditional UPDATE matched 0 rows."
    )


# ---------------------------------------------------------------------------
# Cluster Q — concurrent /workflows/start
# ---------------------------------------------------------------------------


async def test_concurrent_workflows_start_no_session_pool_starvation(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """Audit §2.1 (cluster F) regression guard.

    Pre-fix the route held a SQLAlchemy session across ``await
    composer.compose(...)``, pinning a pooled connection for the
    duration of the (potentially-long) LLM call. Under load, the
    connection pool would starve.

    Cluster F's fix uses ``get_session_factory`` to scope each
    session to a single non-async block, releasing the connection
    BEFORE the await. This test issues N concurrent requests and
    asserts:
      1. None time out at the test level (i.e., no deadlock).
      2. All N succeed (status="running" or "paused").
      3. All N return distinct run_ids.
      4. Run row count in the DB matches N.

    The composer is stubbed (placeholder LLM) to keep the test fast
    and CI-runnable. The session-factory contract is what's load-
    bearing here, not LLM throughput.
    """
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.composition.composer import Composer

    REPO_ROOT = Path(__file__).resolve().parents[2]
    composer_config = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "composer_config.yml"
    )
    policy_path = REPO_ROOT / "configs" / "approval_policy.yml"

    session_factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(session_factory)
    store = ArtifactStore(session_factory=session_factory, recorder=recorder)
    composer = Composer.from_config(composer_config)
    composer._artifact_store = store  # noqa: SLF001

    # Stub the LLM so each compose() returns instantly with a small,
    # valid composed-only YAML. We're testing the session lifecycle,
    # not the LLM.
    canned_yaml = (
        "```yaml\n"
        "name: concurrency_test\n"
        "description: \"smoke\"\n"
        "version: \"0.1.0\"\n"
        "steps:\n"
        "  extract:\n"
        "    class: \"apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep\"\n"
        "    config: \"steps/entity_extraction.yml\"\n"
        "links: {}\n"
        "```\n"
    )

    class _StubLLM:
        def invoke(self, _msgs):
            class _R:
                content = canned_yaml
            return _R()

    composer._llm_factory = lambda **_kw: _StubLLM()  # noqa: SLF001

    policy = ApprovalPolicy.load(policy_path)

    app = create_app(
        engine=cp_engine,
        composer=composer,
        approval_policy=policy,
    )
    transport = httpx.ASGITransport(app=app)

    N = 5

    async def _start_one(idx: int) -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/workflows/start",
                json={
                    "description": f"concurrent test {idx}",
                    "user_id": f"user-{idx}",
                },
            )

    # Fire N concurrent start_workflow calls.
    responses = await asyncio.wait_for(
        asyncio.gather(*[_start_one(i) for i in range(N)]),
        timeout=60.0,  # generous; actual stubbed-LLM run is sub-second
    )

    # All N succeeded.
    for idx, r in enumerate(responses):
        assert r.status_code == 200, (
            f"request {idx} failed: status={r.status_code} body={r.text!r}"
        )

    # All N have distinct run_ids and valid statuses.
    run_ids = [r.json()["run"]["id"] for r in responses]
    statuses = [r.json()["run"]["status"] for r in responses]
    assert len(set(run_ids)) == N, (
        f"expected {N} distinct run_ids, got {len(set(run_ids))}: {run_ids}"
    )
    assert all(s in {"running", "paused"} for s in statuses), (
        f"unexpected statuses: {statuses}"
    )

    # DB count matches.
    with cp_engine.connect() as conn:
        db_count = conn.execute(
            text("SELECT COUNT(*) FROM run WHERE user_id LIKE 'user-%'")
        ).scalar_one()
    assert db_count == N, (
        f"DB has {db_count} runs, expected {N}; some session writes "
        "were lost — session-factory regression?"
    )


# ---------------------------------------------------------------------------
# Cluster R — provenance-recorder threading.Lock under async load
# ---------------------------------------------------------------------------


async def test_provenance_recorder_concurrent_appends_no_chain_break(
    cp_engine: Engine,
) -> None:
    """Audit §2.5 — ProvenanceRecorder.record() uses a
    ``threading.Lock``. The lock acquisition is synchronous; under
    async dispatch, multiple coroutines on the same event loop can
    serialize on it without losing the prev-event-hash chain.

    This test fires K=20 concurrent ``record()`` calls for the same
    run, then asserts:
      1. All K events landed in the DB.
      2. Each event's ``prev_event_hash`` matches the previous
         event's ``event_hash`` (chain integrity).
      3. The chain has no forks (every event has at most one
         predecessor).

    What this test does NOT prove: freedom from event-loop blocking
    pauses. ``threading.Lock`` does block the event loop while a
    coroutine holds it; that's a latency concern, not a correctness
    one. If/when the recorder switches to ``anyio.Lock``, this test
    should still pass.

    Run on a fresh engine so the chain we inspect is exactly the
    events this test wrote.
    """
    # Seed a Run row so FK is satisfied.
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    session_factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(session_factory)

    K = 20

    async def _append(idx: int) -> None:
        # ``record`` is sync — wrap with run_in_executor so multiple
        # appends actually overlap on the event loop. Direct sync
        # calls would just serialize without any concurrency.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.STEP_COMPLETED,
                actor="async-test",
                payload={"i": idx},
            ),
        )

    await asyncio.wait_for(
        asyncio.gather(*[_append(i) for i in range(K)]),
        timeout=15.0,
    )

    # Walk the chain in the DB.
    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT event_hash, prev_event_hash "
                    "FROM provenance_event "
                    "WHERE run_id = :rid "
                    "ORDER BY timestamp, event_hash"
                ),
                {"rid": str(run_id)},
            )
        )

    assert len(rows) == K, (
        f"expected {K} events in DB, got {len(rows)}; some lock-contended "
        "appends were lost?"
    )

    # The first event must have prev=NULL; subsequent events form a chain.
    # Multiple "first" events would mean the lock failed to serialize.
    null_prevs = [r for r in rows if r[1] is None]
    assert len(null_prevs) == 1, (
        f"chain has {len(null_prevs)} events with prev_event_hash=NULL; "
        "exactly 1 expected (the genesis). >1 = the lock failed and "
        "concurrent recorders both saw an empty chain."
    )

    # Every non-null prev_event_hash must match SOME other event's
    # event_hash, and no two events share the same prev (no forks).
    hashes = {r[0] for r in rows}
    prev_counts: dict[str, int] = {}
    for h, prev in rows:
        if prev is None:
            continue
        assert prev in hashes, (
            f"event {h!r} references prev={prev!r} which doesn't exist "
            "in this run's chain — chain corruption."
        )
        prev_counts[prev] = prev_counts.get(prev, 0) + 1

    duplicates = {p: c for p, c in prev_counts.items() if c > 1}
    assert not duplicates, (
        f"chain has fork(s): prev_event_hash referenced more than once: "
        f"{duplicates}. Lock-serialization failed."
    )


# ---------------------------------------------------------------------------
# Cluster S — MCP tool repeat invocations + parallel dispatch
# ---------------------------------------------------------------------------


async def test_mcp_tool_singleton_survives_repeat_calls(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """The MCP-side ``ControlPlaneClient`` is a process-level
    singleton (``_shared._client``). Cluster O fixed the ""Event loop
    is closed"" bug by isolating the startup-health-check client
    from the singleton, but the singleton itself must still survive
    repeat tool calls within FastMCP's running event loop.

    Test: build an in-process MCP-tool fixture wired to a real
    ASGI app, call ``estimate_cost`` and ``confirm_allocation`` 5
    times each (alternating to stress connection-reuse), and assert
    every call succeeds. If the AsyncClient's connection pool ever
    decides to close mid-test, the second call would see a
    "connection closed" error.
    """
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.tools import _shared
    from apecx_integration.mcp_surface.tools.hpc import (
        confirm_allocation,
        estimate_cost,
    )

    # Seed a Run + workflow artifact so /hpc/estimate has something
    # real to compute against (the route loads the workflow YAML to
    # count steps).
    run_id, _artifact_id = _seed_run_with_workflow_artifact(
        cp_engine, tmp_path / "wf_repeat.yml"
    )

    app = create_app(engine=cp_engine)

    # Wire a singleton ControlPlaneClient pointing at the in-process
    # ASGI transport — exactly what tools/_shared.set_client expects.
    cp_client = ControlPlaneClient("http://test")
    cp_client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    _shared.set_client(cp_client)

    try:
        # Alternate two tools, 5 rounds. If the singleton's
        # connection pool dies between calls we'd see it here.
        for round_idx in range(5):
            est = await estimate_cost(run_id=str(run_id))
            assert "total_core_hours" in est, (
                f"round {round_idx} estimate_cost failed: {est!r}"
            )
            conf = await confirm_allocation(
                run_id=str(run_id),
                confirmed_core_hours=est["total_core_hours"],
            )
            assert conf.get("confirmed") is True, (
                f"round {round_idx} confirm_allocation failed: {conf!r}"
            )
    finally:
        await cp_client.close()
        _shared.set_client(None)


async def test_mcp_tools_parallel_dispatch_no_singleton_corruption(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """Two MCP tool calls fired concurrently against the same
    singleton ``ControlPlaneClient`` must both complete cleanly.
    httpx's AsyncClient supports concurrent requests over its
    connection pool; if the singleton's transport were
    accidentally not thread-safe (e.g., a hand-rolled wrapper that
    serializes), a parallel dispatch would either deadlock or
    corrupt one of the responses.

    Real MCP clients (Claude Desktop) typically dispatch tools
    serially, but the architecture should not depend on that.
    """
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.tools import _shared
    from apecx_integration.mcp_surface.tools.hpc import estimate_cost

    # Two distinct runs, each with a workflow artifact, so the
    # route does real per-run work.
    run_id_a, _ = _seed_run_with_workflow_artifact(
        cp_engine, tmp_path / "wf_a.yml"
    )
    run_id_b, _ = _seed_run_with_workflow_artifact(
        cp_engine, tmp_path / "wf_b.yml"
    )
    run_ids = [run_id_a, run_id_b]

    app = create_app(engine=cp_engine)
    cp_client = ControlPlaneClient("http://test")
    cp_client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    _shared.set_client(cp_client)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                estimate_cost(run_id=str(run_ids[0])),
                estimate_cost(run_id=str(run_ids[1])),
            ),
            timeout=10.0,
        )
    finally:
        await cp_client.close()
        _shared.set_client(None)

    assert all("total_core_hours" in r for r in results), (
        f"parallel dispatch produced malformed responses: {results}"
    )
    # The two responses should be functionally identical (same
    # workflow shape) — important not for the assertion, but to
    # confirm neither was contaminated by the other.
    assert results[0]["total_core_hours"] == results[1]["total_core_hours"]
#
# Earlier draft tried to assert "event loop heartbeat doesn't skip
# ticks under sync-locked ProvenanceRecorder.record() load", but on
# SQLite each record() takes <1ms, so 30 of them complete before a
# 50ms heartbeat can fire — test is structurally flaky / vacuous.
# The real perf concern (lock blocks event loop) requires either
# slow disk simulation or genuine multi-coroutine contention with
# longer hold times — neither of which a unit test can fairly
# stage.
#
# If audit §2.5 is ever escalated from MEDIUM to a real fix, the
# load test belongs in a benchmark suite (pytest-benchmark or a
# dedicated harness), not in pytest tests/.
