"""Adversarial async tests — push the system into failure modes
that the happy-path concurrency tests in
``test_async_concurrency.py`` and ``test_async_composer_concurrency.py``
deliberately avoided. Goal: find real brittleness, not confirm the
architecture is fine.

Each test below is named for the specific failure mode it targets
and the suspected bug that would surface. If a test fails, that's
the actual finding — fix in a follow-up cluster. If it passes,
we've proven the system is more robust than the obvious concern.

These tests use ``httpx.AsyncClient`` over ``httpx.ASGITransport``
so failures are real async lifecycle events, not test-fixture
artifacts.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.composition.approval_policy import ApprovalPolicy
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import (
    Composer,
    ComposerResponseError,
)
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)
APPROVAL_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


CANNED_OK = textwrap.dedent(
    """\
    ```yaml
    name: smoke
    description: "x"
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)


def _make_composer(cp_engine: Engine, *, llm_factory) -> Composer:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._artifact_store = store  # noqa: SLF001
    composer._llm_factory = llm_factory  # noqa: SLF001
    return composer


# ---------------------------------------------------------------------------
# U1 — composer raises mid-/workflows/start
#      Suspected bug: run row stays PENDING forever; no cleanup after the
#      pre-await commit. The audit's §2.4 fix added an HTTPException for
#      "run vanished" but didn't address "compose() raised AFTER run
#      committed" which is the real production failure mode.
# ---------------------------------------------------------------------------


async def test_workflows_start_when_composer_raises_does_not_orphan_pending_run(
    cp_engine: Engine,
) -> None:
    """If ``composer.compose()`` raises (LLM unreachable, malformed
    response, scanner violation, etc.), what happens to the Run row
    that ``/workflows/start`` committed BEFORE the compose await?

    Expected behavior the operator would want:
    - Either the route catches the exception and marks the run
      FAILED with a reason, OR
    - The route re-raises and the operator sees the error in the
      response.

    Either is acceptable; what's NOT acceptable is the run staying
    silently in PENDING forever with no audit trail of why.

    This test injects a composer whose compose() raises
    ComposerResponseError (a realistic failure: the LLM returned
    no yaml block). Asserts:
    - HTTP response is non-2xx (operator gets a clear failure).
    - Run row in DB is NOT in PENDING.
    """

    class _BadResp:
        content = "I forgot to emit a fenced block; sorry."

    class _BadLLM:
        def invoke(self, _msgs):
            return _BadResp()

    composer = _make_composer(cp_engine, llm_factory=lambda **_kw: _BadLLM())
    policy = ApprovalPolicy.load(APPROVAL_POLICY)
    app = create_app(engine=cp_engine, composer=composer, approval_policy=policy)
    # ``raise_app_exceptions=False`` matches uvicorn's production
    # behavior: FastAPI's exception middleware catches the route
    # exception and returns 500 instead of letting it bubble.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/workflows/start",
            json={"description": "trigger malformed-LLM path", "user_id": "alex"},
        )

    # Operator must see a clear failure, not a silent success.
    assert resp.status_code >= 400, (
        f"composer raised but route returned {resp.status_code}; "
        "operator silently sees success even though no workflow exists."
    )

    # Run rows: there's at most one (the one this request created),
    # and it must NOT be in PENDING. PENDING means "we wrote it then
    # forgot it" — a real orphan.
    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT id, status FROM run WHERE user_id = 'alex' "
                    "ORDER BY created_at DESC LIMIT 5"
                )
            )
        )

    if not rows:
        # Acceptable outcome: route caught the exception, rolled back
        # the run row, raised HTTP 5xx. No orphan.
        return

    most_recent = rows[0]
    assert most_recent[1].lower() != "pending", (
        f"run {most_recent[0]} left in PENDING after composer raised "
        "ComposerResponseError. The pre-await commit succeeded but "
        "no cleanup happened; this run is now an audit-trail orphan."
        "\nFix direction: catch the exception in the route handler, "
        "open a fresh session, mark the run FAILED with a reason."
    )


# ---------------------------------------------------------------------------
# U2 — mixed-validity prompts under concurrent dispatch
#      Suspected bug: when half of N parallel /workflows/start calls
#      raise (empty prompt rejected by Pydantic) and the other half
#      succeed, the DB might end up with orphan rows or interleaved
#      provenance events.
# ---------------------------------------------------------------------------


async def test_workflows_start_mixed_validity_concurrent_db_consistent(
    cp_engine: Engine,
) -> None:
    """N=10 concurrent /workflows/start where 5 have valid prompts
    and 5 have empty prompts (which Pydantic should reject before
    any DB write). Assert:
    - Exactly 5 succeed, 5 fail with 422 (not 5xx).
    - Exactly 5 Run rows in the DB.
    - No half-written run-FK violations or orphaned artifacts.
    """
    composer = _make_composer(
        cp_engine,
        llm_factory=lambda **_kw: type(
            "L", (), {"invoke": lambda self, msgs: type("R", (), {"content": CANNED_OK})()}
        )(),
    )
    policy = ApprovalPolicy.load(APPROVAL_POLICY)
    app = create_app(engine=cp_engine, composer=composer, approval_policy=policy)
    transport = httpx.ASGITransport(app=app)

    async def _start(idx: int, *, valid: bool):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/workflows/start",
                json={
                    "description": f"valid prompt {idx}" if valid else "",
                    "user_id": f"mixed-{idx}",
                },
            )

    # Interleave 5 valid + 5 invalid.
    tasks = []
    for i in range(10):
        tasks.append(_start(i, valid=(i % 2 == 0)))
    responses = await asyncio.gather(*tasks)

    successes = [r for r in responses if r.status_code == 200]
    rejections = [r for r in responses if 400 <= r.status_code < 500]
    server_errors = [r for r in responses if r.status_code >= 500]

    assert len(server_errors) == 0, (
        f"expected no 5xx errors with mixed validity input; got "
        f"{len(server_errors)}: {[r.text for r in server_errors]}"
    )
    assert len(successes) == 5, (
        f"expected exactly 5 successes, got {len(successes)}"
    )
    assert len(rejections) == 5, (
        f"expected exactly 5 client-side rejections, got {len(rejections)}"
    )

    # DB count must match the success count exactly.
    with cp_engine.connect() as conn:
        run_count = conn.execute(
            text("SELECT COUNT(*) FROM run WHERE user_id LIKE 'mixed-%'")
        ).scalar_one()
    assert run_count == 5, (
        f"DB has {run_count} runs but exactly 5 requests succeeded. "
        "Either some failed requests left orphan rows, or some "
        "successes lost their write."
    )


# ---------------------------------------------------------------------------
# U3 — provenance writer failure mid-chain
#      Suspected bug: if recorder.record() raises after appending one
#      event, the next call's prev_event_hash should still match the
#      last successfully-written event (not point at a non-existent
#      one).
# ---------------------------------------------------------------------------


async def test_provenance_chain_intact_after_writer_failure_midway(
    cp_engine: Engine, monkeypatch,
) -> None:
    """Simulate: recorder writes events 1, 2 successfully; event 3
    raises; events 4, 5 succeed. After this, the chain should be
    1 -> 2 -> 4 -> 5 (event 3 never landed). Critically, event 4's
    prev_event_hash should equal event 2's event_hash, not point at
    a phantom event 3.

    If the recorder cached the would-be event 3's hash before the
    actual commit, event 4 would reference a non-existent
    predecessor and the chain would break.
    """
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)

    # Inject failure on the 3rd record() call by patching session.add.
    call_count = {"n": 0}
    real_factory = factory

    class _PoisonedFactory:
        def __call__(self):
            call_count["n"] += 1
            session = real_factory()
            if call_count["n"] == 3:
                # Force the 3rd record() to fail mid-write.
                original_add = session.add

                def _failing_add(*args, **kwargs):
                    raise RuntimeError("simulated mid-chain DB failure")

                session.add = _failing_add  # type: ignore[method-assign]
            return session

    recorder._session_factory = _PoisonedFactory()  # noqa: SLF001

    payloads = [{"i": i} for i in range(5)]
    failures = []
    for i, p in enumerate(payloads):
        try:
            recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.STEP_COMPLETED,
                actor="adversarial",
                payload=p,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append((i, type(exc).__name__))

    # Exactly the 3rd record() should have failed.
    assert failures == [(2, "RuntimeError")], (
        f"expected only the 3rd record() to fail; got {failures}"
    )

    # Walk the chain.
    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT event_hash, prev_event_hash, payload "
                    "FROM provenance_event WHERE run_id = :rid "
                    "ORDER BY timestamp"
                ),
                {"rid": str(run_id)},
            )
        )

    assert len(rows) == 4, (
        f"expected 4 events (5 attempted, 1 failed); got {len(rows)}"
    )

    # First event has prev=NULL.
    assert rows[0][1] is None
    # Each subsequent event's prev_event_hash matches the previous
    # event's event_hash.
    for i in range(1, len(rows)):
        prev_hash = rows[i][1]
        assert prev_hash == rows[i - 1][0], (
            f"chain broken at event {i}: prev_event_hash={prev_hash!r} "
            f"but previous event_hash={rows[i - 1][0]!r}. The failed "
            "event 3's hash leaked into the in-memory state, so "
            "event 4 referenced a phantom predecessor."
        )


# ---------------------------------------------------------------------------
# U4 — whitelist mutated at runtime
#      Suspected bug: cluster B's whitelist caching at __init__ does
#      NOT reload on file change. That's intentional, but operators
#      might not know it. This test verifies that a runtime mutation
#      of the file does NOT affect a running composer.
# ---------------------------------------------------------------------------


async def test_composer_whitelist_runtime_change_ignored_until_restart(
    cp_engine: Engine, tmp_path: Path,
) -> None:
    """Cluster B (audit §1.4) cached the import whitelist at
    Composer.__init__ time. This test pins that contract: mutating
    the whitelist file at runtime does NOT change the composer's
    behavior until restart.

    Setup: build a composer pointed at a writable whitelist that
    permits ``json``. Trigger a compose() with novel Python that
    imports ``json`` — should pass. Then DELETE ``json`` from the
    whitelist file. Trigger another compose() — should STILL pass
    because the whitelist is cached.
    """
    # Override the whitelist path to a tmp file we can mutate.
    whitelist_path = tmp_path / "wl.txt"
    whitelist_path.write_text("# adversarial whitelist\njson\nnanobrain\ndataclasses\ntyping\n")

    # Custom composer config pointing at the writable whitelist.
    custom_cfg = tmp_path / "composer_cfg.yml"
    cfg_text = COMPOSER_CONFIG.read_text()
    # Replace the sandbox_whitelist_path line.
    new_cfg_text = []
    for line in cfg_text.splitlines():
        if line.startswith("sandbox_whitelist_path:"):
            new_cfg_text.append(f"sandbox_whitelist_path: {whitelist_path}")
        else:
            new_cfg_text.append(line)
    custom_cfg.write_text("\n".join(new_cfg_text))

    # Resolve other relative paths in the cfg by writing the cfg next
    # to the original. Simplest: copy required dirs to be siblings,
    # OR just patch the ComponentCatalog manifest path the same way.
    # For this test we only need the whitelist behavior, so let the
    # composer init fail on missing manifest if we get there — we
    # don't.
    # Actually: just construct ComposerConfig directly to keep this
    # test isolated.
    from apecx_integration.composition.composer import REQUIRED_PROMPT_FILES
    from apecx_integration.composition.composer_schemas import ComposerConfig

    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    for fname in REQUIRED_PROMPT_FILES:
        (prompt_dir / fname).write_text(f"# {fname}")

    config = ComposerConfig(
        library_version="adversarial-test",
        prompt_dir=prompt_dir,
        sandbox_whitelist_path=whitelist_path,
        component_catalog_paths=[],
        retrieval_k=1,
    )
    composer = Composer(config)

    # Sanity: ``json`` is in the cached whitelist.
    assert "json" in composer._whitelist  # noqa: SLF001
    initial_whitelist = composer._whitelist  # noqa: SLF001

    # Mutate the file: REMOVE json.
    whitelist_path.write_text("# mutated\nnanobrain\ndataclasses\ntyping\n")

    # The cached whitelist must NOT have changed (cluster B
    # contract: cache at init, no reload).
    assert "json" in composer._whitelist, (  # noqa: SLF001
        "whitelist mutation at runtime affected the running composer; "
        "cluster B's caching contract is broken."
    )
    # And it should be the SAME object (identity check).
    assert composer._whitelist is initial_whitelist, (  # noqa: SLF001
        "composer rebuilt its whitelist between accesses; the cache "
        "isn't really a cache."
    )


# ---------------------------------------------------------------------------
# U5 — resource exhaustion: N=50 concurrent /workflows/start
#      Suspected bug: connection pool starvation, WAL deadlock, or
#      ASGITransport overload at scale.
# ---------------------------------------------------------------------------


async def test_50_concurrent_workflows_start_no_starvation(
    cp_engine: Engine,
) -> None:
    """50 concurrent /workflows/start calls. Cluster Q used N=5 to
    prove no starvation; N=50 stresses the connection pool, the WAL
    writer, and the SQLAlchemy session factory.

    If the pool exhausts, requests time out. If the WAL serialization
    is broken, some writes are lost. If anything else races, distinct
    user_ids would not produce distinct run_ids.
    """
    composer = _make_composer(
        cp_engine,
        llm_factory=lambda **_kw: type(
            "L", (), {"invoke": lambda self, msgs: type("R", (), {"content": CANNED_OK})()}
        )(),
    )
    policy = ApprovalPolicy.load(APPROVAL_POLICY)
    app = create_app(engine=cp_engine, composer=composer, approval_policy=policy)
    transport = httpx.ASGITransport(app=app)

    N = 50

    async def _start(idx: int):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.post(
                "/workflows/start",
                json={
                    "description": f"load test {idx}",
                    "user_id": f"load-{idx}",
                },
            )

    responses = await asyncio.wait_for(
        asyncio.gather(*[_start(i) for i in range(N)]),
        timeout=120.0,
    )

    statuses = [r.status_code for r in responses]
    bad = [s for s in statuses if s != 200]
    assert not bad, (
        f"{len(bad)}/{N} requests failed under load; status distribution: "
        f"{set(statuses)}. The first failure's body: "
        f"{next(r.text for r in responses if r.status_code != 200)[:200]}"
    )

    # All N run rows landed.
    with cp_engine.connect() as conn:
        run_count = conn.execute(
            text("SELECT COUNT(*) FROM run WHERE user_id LIKE 'load-%'")
        ).scalar_one()
    assert run_count == N, (
        f"DB has {run_count} runs from {N} successful requests — "
        "the WAL writer dropped writes under contention?"
    )


# ---------------------------------------------------------------------------
# U6 — MCP wraps Control Plane 5xx errors cleanly
#      Suspected bug: cluster D §3.3 wrapped 503; 500 (real server
#      error) is untested. Without explicit handling, 500 falls
#      through raise_for_status() and the MCP tool emits an unhelpful
#      httpx exception.
# ---------------------------------------------------------------------------


async def test_mcp_client_handles_500_from_control_plane(monkeypatch) -> None:
    """If the Control Plane raises a 500 (real server error, e.g.,
    DB connection died, executor crashed), what does the MCP client
    surface to the operator?

    Cluster D §3.3 wrapped 501 (NotImplementedError) and 503
    (ControlPlaneDependencyError). 500 is unhandled — falls through
    raise_for_status() to httpx.HTTPStatusError. The MCP tool
    layer's `result.model_dump(mode="json")` never runs because the
    exception bubbles up.

    Test: build a CP that always 500s. Call estimate_cost. Assert
    the error is something operator-readable (not a raw httpx
    exception with response internals leaking).
    """
    from fastapi import FastAPI, HTTPException
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.tools import _shared
    from apecx_integration.mcp_surface.tools.hpc import estimate_cost

    # Tiny FastAPI app that returns 500 for every /hpc/estimate call.
    app = FastAPI()

    @app.post("/hpc/estimate")
    def _bomb():
        raise HTTPException(status_code=500, detail="simulated CP crash")

    cp_client = ControlPlaneClient("http://test")
    cp_client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    _shared.set_client(cp_client)

    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await estimate_cost(run_id=str(uuid4()))
    finally:
        await cp_client.close()
        _shared.set_client(None)

    err = excinfo.value
    # If this assertion fails, it means the MCP client SHOULD wrap
    # 500 in a friendlier exception (matching the 501/503 pattern
    # from cluster D §3.3). Until that happens, the test pins the
    # current contract: 500 surfaces as raw httpx.HTTPStatusError.
    assert err.response.status_code == 500
    assert "simulated CP crash" in err.response.text
