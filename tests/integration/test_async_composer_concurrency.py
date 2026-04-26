"""Concurrent ``Composer.compose()`` calls must not corrupt
shared state.

The Composer is constructed once per Control Plane process and
shared across every HTTP/MCP request that hits ``/workflows/start``
or ``/workflows/plan``. Each ``compose()`` call:

- reads the shared ``_prompts`` / ``_catalog`` / ``_whitelist`` /
  ``_rag_index`` (all initialized at __init__ and treated as
  read-only);
- builds a fresh per-call ``_llm_factory()`` LLM instance;
- runs the T13 scanner over any novel Python (read-only against
  ``_whitelist``);
- categorizes the workflow (read-only against
  ``catalog_yaml_paths`` derived from retrieval hits);
- persists via the shared ``_artifact_store`` (which has its own
  internal session/transaction story, and writes one row per
  call).

The audit covered route-level concurrency (clusters E, F) and
recorder-level concurrency (cluster R), but never directly
exercised concurrent ``compose()`` calls. This file fills that
gap.

These tests are intentionally separate from
``test_async_concurrency.py`` — that file is about HTTP-level
race conditions; this one is about the composer object itself.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)


def _make_composer_with_canned_response(
    cp_engine: Engine, canned: str
) -> Composer:
    """Build a real Composer with a canned-LLM factory and a real
    ArtifactStore wired against the test engine.
    """
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._artifact_store = store  # noqa: SLF001

    class _R:
        content = canned

    class _StubLLM:
        def invoke(self, _msgs):
            return _R()

    composer._llm_factory = lambda **_kw: _StubLLM()  # noqa: SLF001
    return composer


def _seed_run(cp_engine: Engine, *, user_id: str = "alex") -> UUID:
    """Insert a Run row so the composer's ArtifactStore has a valid
    FK target. Returns the run_id.
    """
    from datetime import UTC, datetime

    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PENDING', :ts)"
            ),
            {
                "id": str(run_id),
                "uid": user_id,
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    return run_id


CANNED_COMPOSED_ONLY = textwrap.dedent(
    """\
    ```yaml
    name: concurrency_smoke
    description: "two-step composed-only smoke"
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
      rank:
        class: "nanobrain.library.workflows.viral_protein_analysis.steps.result_collection_step.ResultCollectionStep"
        config: "steps/result_ranking.yml"
    links:
      extract_to_rank:
        class: "nanobrain.core.link.DirectLink"
        config:
          link_type: direct
          source: "extract.entity_candidates_output"
          target: "rank.results_input"
    ```
    """
)


async def test_composer_concurrent_compose_no_artifact_collisions(
    cp_engine: Engine,
) -> None:
    """N concurrent compose() calls must persist N distinct
    artifacts and produce N distinct run_ids without state
    cross-contamination. If the composer accidentally shared
    per-call state via attribute mutation (e.g., self._last_yaml),
    parallel calls would race and one run's metadata would land
    on another run's artifact.
    """
    composer = _make_composer_with_canned_response(
        cp_engine, CANNED_COMPOSED_ONLY
    )

    N = 5
    run_ids = [_seed_run(cp_engine, user_id=f"concurrent-{i}") for i in range(N)]

    async def _one(run_id: UUID, idx: int):
        return await composer.compose(
            f"prompt {idx}",
            context={"run_id": run_id},
        )

    results = await asyncio.wait_for(
        asyncio.gather(*[_one(rid, i) for i, rid in enumerate(run_ids)]),
        timeout=30.0,
    )

    # Every result has a distinct artifact_id.
    artifact_ids = [r.artifact_id for r in results]
    assert len(set(artifact_ids)) == N, (
        f"composer produced overlapping artifact_ids: {artifact_ids}. "
        "Parallel compose() calls likely shared mutable state."
    )

    # Every result's yaml_bytes is byte-identical (canned response is
    # the same) — proves no inter-call corruption of the parsed body.
    yaml_bytes_set = {bytes(r.yaml_bytes) for r in results}
    assert len(yaml_bytes_set) == 1, (
        f"composer produced {len(yaml_bytes_set)} distinct yaml_bytes "
        "across N concurrent calls with the same canned response — "
        "shared parser state was mutated by a parallel call."
    )

    # Every artifact landed in the DB with the right run linkage.
    with cp_engine.connect() as conn:
        for rid, aid in zip(run_ids, artifact_ids):
            row = conn.execute(
                text(
                    "SELECT run_id FROM artifact WHERE id = :aid"
                ),
                {"aid": str(aid)},
            ).first()
            assert row is not None, (
                f"artifact {aid} not persisted for run {rid}"
            )
            # Note: ArtifactStore writes the artifact's FK from the
            # ``run_id`` context kwarg, so this should match.
            assert UUID(row[0]) == rid, (
                f"artifact {aid} persisted under run {row[0]}, "
                f"expected {rid} — context-kwarg leakage between "
                "concurrent compose() calls."
            )


async def test_composer_concurrent_compose_summary_unique_per_call(
    cp_engine: Engine,
) -> None:
    """Each compose() returns a CompositionSummary with a
    summary_sentence and review_notes. Concurrent calls with the
    same canned response should each get their own summary object;
    if the composer accidentally aliased the summary across calls,
    one call's review_notes would mutate the others'.
    """
    composer = _make_composer_with_canned_response(
        cp_engine, CANNED_COMPOSED_ONLY
    )

    N = 3
    run_ids = [_seed_run(cp_engine, user_id=f"summary-{i}") for i in range(N)]

    async def _one(rid: UUID):
        return await composer.compose("p", context={"run_id": rid})

    results = await asyncio.gather(*[_one(rid) for rid in run_ids])

    # Each summary is its own object, even though the contents are
    # identical (same canned YAML).
    summaries = [r.composition_summary for r in results]
    ids = [id(s) for s in summaries]
    assert len(set(ids)) == N, (
        "compose() aliased the same CompositionSummary across calls; "
        "mutating one would mutate all."
    )


async def test_composer_canned_response_paths_dont_share_llm_instances(
    cp_engine: Engine,
) -> None:
    """The composer calls ``self._llm_factory(...)`` ONCE per
    compose() (line 355 of composer.py). Each call must get its own
    LLM instance, not a shared one. If the factory accidentally
    returned a singleton, two concurrent compose()s would step on
    each other's ``llm.invoke()`` calls.

    Test by injecting a factory that records every call's id() into
    a shared set and asserts the count matches the number of
    concurrent compose() invocations.
    """
    composer = Composer.from_config(COMPOSER_CONFIG)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    composer._artifact_store = ArtifactStore(  # noqa: SLF001
        session_factory=factory, recorder=recorder
    )

    factory_call_count = 0

    class _R:
        content = CANNED_COMPOSED_ONLY

    class _StubLLM:
        def invoke(self, _msgs):
            return _R()

    def _factory(**_kw):
        nonlocal factory_call_count
        factory_call_count += 1
        return _StubLLM()

    composer._llm_factory = _factory  # noqa: SLF001

    N = 4
    run_ids = [_seed_run(cp_engine, user_id=f"llm-{i}") for i in range(N)]

    async def _one(rid: UUID):
        return await composer.compose("p", context={"run_id": rid})

    await asyncio.gather(*[_one(rid) for rid in run_ids])

    # NOTE: counting calls, not id()'s — short-lived instances can
    # share Python's id() across sequential allocations (memory
    # address reuse after GC), so id() collision is NOT evidence of
    # a singleton bug. The factory call count is the load-bearing
    # invariant: "factory called once per compose()".
    assert factory_call_count == N, (
        f"_llm_factory was called {factory_call_count} times for "
        f"{N} compose() calls; should be 1:1. Either the factory "
        "is being skipped on some path, or compose() short-circuited."
    )
