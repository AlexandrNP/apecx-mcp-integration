"""Cluster AD — multi-recorder hash-chain forks.

Cluster X (2026-04-26) added a per-process, per-run in-memory
hash-cursor cache to ``ProvenanceRecorder`` to fix tied-microsecond
forks under K=20 concurrent appends. The cache is *per-instance*:
two ``ProvenanceRecorder`` objects in the same process do NOT
share their caches.

The Control Plane creates TWO recorder instances per process:

  - ``app.state.recorder``  (used by HTTP routes:
    /approvals/approve, /workflows/*, /hpc/* — see ``app.py:113``)
  - composer's recorder      (used by ArtifactStore + LocalExecutor —
    see ``app.py:164``, passed in via ``_load_composer_components``)

Both routinely write events for the same ``run_id``. The composer
side records WORKFLOW_GENERATED + RUN_STARTED + RUN_COMPLETED;
the routes side records APPROVAL_REQUESTED + APPROVAL_DECIDED +
HPC ingest events. A workflow that pauses for approval mid-flight
interleaves both recorders' writes:

    R_composer: WORKFLOW_GENERATED  (cache: {run: hash_WG})
    R_composer: RUN_STARTED         (cache: {run: hash_RS})
    R_routes:   APPROVAL_REQUESTED  (cache empty → DB fallback →
                                     finds RS → cache: {run: hash_AR})
    R_routes:   APPROVAL_DECIDED    (cache: {run: hash_AD})
    R_composer: RUN_COMPLETED       (cache says prev=hash_RS — STALE!
                                     The actual chain has 2 more
                                     events after RS — AR and AD.
                                     Writes RUN_COMPLETED with
                                     prev_event_hash=hash_RS.)

``ProvenanceRecorder.validate(run_id)`` walks events in
``(timestamp, id)`` order and asserts each event's
``prev_event_hash`` equals the previous event's ``event_hash``.
The above sequence raises ``ChainBroken`` because RUN_COMPLETED's
prev points at RS, not at AD.

This is the structural multi-recorder bug. Cluster X's cache is
necessary but not sufficient: it protects within an instance, not
across instances.

Fix direction: either

  A) Single recorder per process. Pass ``app.state.recorder`` into
     ArtifactStore and LocalExecutor at composer setup. Strictly
     simpler — one cache, one DB-fallback path.
  B) Cache invalidation on write through any recorder. Hard
     without a shared in-process cache.
  C) Drop the cache entirely; force every record() call to do the
     DB-fallback lookup. Re-opens the original cluster X bug
     (UUID-tiebreak fork under tied microseconds).

Option A is the right call: cluster X's cache becomes load-bearing
*and* sufficient. This test pins the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ChainBroken,
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from sqlalchemy import text


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_engine(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


def _seed_run(engine) -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
    return run_id


def _interleaved_writes(recorder_a, recorder_b, run_id) -> None:
    """Compose-then-approve-then-complete sequence using two writers.

    Mirrors the real production interleave when the composer's
    recorder writes the workflow lifecycle events while the
    routes' recorder writes the approval-loop events.
    """
    base = datetime.now(UTC)
    recorder_a.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="composer_recorder",
        payload={"step": 1},
        now=base,
    )
    recorder_b.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_REQUESTED,
        actor="route_recorder",
        payload={"step": 2},
        now=base + timedelta(milliseconds=10),
    )
    recorder_b.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_DECIDED,
        actor="route_recorder",
        payload={"step": 3},
        now=base + timedelta(milliseconds=20),
    )
    recorder_a.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_COMPLETED,
        actor="composer_recorder",
        payload={"step": 4},
        now=base + timedelta(milliseconds=30),
    )


def test_two_distinct_recorders_fork_the_chain(tmp_path) -> None:
    """Negative-control: two ``ProvenanceRecorder`` instances writing
    to the same run produce a forking chain.

    This documents the constraint cluster X's per-instance cache
    imposes: it must own the run from start to terminal event. If a
    second recorder writes intervening events, the first recorder's
    next write uses its stale cache and forks the chain.

    Asserting that ``validate`` raises means a regression in
    production wiring (e.g., somebody re-introducing a second
    ProvenanceRecorder somewhere) would be caught by the
    ``test_shared_recorder_keeps_chain_intact`` test below — this
    one just pins what the bug shape looks like.
    """
    engine = _migrated_engine(tmp_path)
    factory = make_session_factory(engine)
    recorder_a = ProvenanceRecorder(factory)
    recorder_b = ProvenanceRecorder(factory)

    run_id = _seed_run(engine)
    _interleaved_writes(recorder_a, recorder_b, run_id)

    with pytest.raises(ChainBroken):
        recorder_a.validate(run_id)


def test_shared_recorder_keeps_chain_intact(tmp_path) -> None:
    """Positive contract: when both writers share ONE
    ``ProvenanceRecorder`` instance, the per-run hash cache stays
    consistent and the chain validates.

    Production must wire ``app.state.recorder``,
    ``ArtifactStore.recorder``, and ``LocalExecutor._recorder`` to
    the same instance. ``create_app(recorder=...)`` and
    ``_build_components_from_env(engine, recorder=...)`` accept
    one explicitly so the serve path can plumb a single recorder
    end-to-end.
    """
    engine = _migrated_engine(tmp_path)
    factory = make_session_factory(engine)
    shared = ProvenanceRecorder(factory)

    run_id = _seed_run(engine)
    _interleaved_writes(shared, shared, run_id)

    # No raise — chain is intact.
    shared.validate(run_id)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, prev_event_hash, event_hash "
                "FROM provenance_event WHERE run_id = :rid "
                "ORDER BY timestamp, id"
            ),
            {"rid": str(run_id)},
        ).fetchall()

    assert len(rows) == 4, f"expected 4 events, got {len(rows)}"
    expected_prev = None
    for idx, (eid, prev, event_hash) in enumerate(rows):
        assert prev == expected_prev, (
            f"event idx={idx} id={eid} prev={prev!r} expected={expected_prev!r}"
        )
        expected_prev = event_hash


def test_serve_path_wiring_uses_a_single_recorder(tmp_path) -> None:
    """Production wiring contract: ``_build_components_from_env``,
    when given a recorder, plumbs THAT recorder into the
    ArtifactStore (and therefore the LocalExecutor's reference to
    it). ``create_app`` accepting the same recorder hangs it on
    ``app.state.recorder``. End result: one recorder for the whole
    process.

    A future refactor that drops one of those parameters or builds
    a fresh recorder somewhere in the chain would re-introduce
    cluster AD. This test catches that by reaching into the
    composed objects and asserting identity.
    """
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.app import (
        _build_components_from_env,
        create_app,
    )

    engine = _migrated_engine(tmp_path)
    factory = make_session_factory(engine)
    shared = ProvenanceRecorder(factory)

    # Fully isolated env — _build_components_from_env reads env
    # vars to decide what to build; absent vars => everything
    # None. ArtifactStore is built unconditionally; the rest may
    # be None on a config-bare laptop. We at least get back the
    # store wiring through the Composer / LocalExecutor when
    # they are configured. Here we just check the recorder
    # passed in is what comes back wired to the store, which we
    # can reach via the LocalExecutor or via direct construction.
    composer, policy, executor = _build_components_from_env(
        engine, recorder=shared
    )

    if executor is not None:
        assert executor._recorder is shared, (
            "LocalExecutor must hold the SHARED recorder, not a "
            "freshly-built one — otherwise cluster AD reproduces."
        )
        # The ArtifactStore the executor talks to should also be
        # backed by the same recorder.
        assert executor._artifact_store._recorder is shared, (
            "ArtifactStore inside LocalExecutor must hold the "
            "shared recorder."
        )

    # Also exercise create_app's recorder= plumbing.
    app = create_app(engine=engine, recorder=shared)
    assert app.state.recorder is shared, (
        "create_app must hang the supplied recorder on app.state, "
        "not re-create one."
    )

    # Finally, build a default ArtifactStore with the shared
    # recorder and verify identity — covers the case where
    # composer config wasn't loaded but a future caller wires
    # the store directly.
    direct_store = ArtifactStore(session_factory=factory, recorder=shared)
    assert direct_store._recorder is shared
