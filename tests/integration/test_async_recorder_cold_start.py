"""Cluster AG — recorder cold-start picks wrong chain tail.

Cluster X added a per-instance ``_last_hash`` cache that fixes
chain forks during a hot-cache write burst. When the cache is
cold (after process restart, OR when a fresh recorder instance
sees a run for the first time), ``record()`` falls back to:

    SELECT * FROM provenance_event
    WHERE run_id = :rid
    ORDER BY timestamp DESC, id DESC
    LIMIT 1

…to find "the latest event" and use its hash as the new event's
prev_event_hash. ``id`` is a random uuid4 — under tied
microsecond timestamps the secondary sort key picks the
lex-largest UUID, which has nothing to do with chronological or
cause-ordered "latest." The cold-start can therefore pick a
non-tail event (a middle-of-chain event that just happened to
have the largest UUID).

Concrete scenario:
  1. Process P1 writes E1, E2, E3, E4, E5 for run R, with
     cluster X cache hot. All 5 events have tied microseconds.
     The chain is genesis E1 → E2 → E3 → E4 → E5 (write order
     by cache).
  2. Among E1..E5, suppose E2 happens to have the lex-largest
     UUID.
  3. Process P1 exits (or the tests construct a fresh recorder).
  4. Process P2 starts a fresh recorder. Cache empty.
  5. record(E6) for the same run R.
     - Cache miss → ``_last_event_for_run`` → SELECT ORDER BY
       (timestamp DESC, id DESC) → returns E2 (lex-largest).
     - E6.prev_event_hash = hash(E2).
  6. DB now has E2 referenced by both E3.prev_event_hash AND
     E6.prev_event_hash → FORK.

Cluster AF's new validate (graph walk) correctly detects the
fork. But it surfaces the bug as a ChainBroken at validate-time
rather than preventing it at write-time. For an audit log, the
audit chain itself being structurally broken is worse than a
late-detected error: any recovery requires reconstructing the
"intended" tail.

Fix: ``_last_event_for_run`` should identify the chain's TAIL
(the unique event whose ``event_hash`` is not referenced by any
other event's ``prev_event_hash``) — the same graph-walk shape
the new validate uses. That requires no additional schema and
no monotonic counter. O(N) per cold-start lookup, but only the
FIRST record() per run pays it; subsequent calls hit the
warm cache.

Test: simulate the scenario by:
  - Writing K=20 concurrent events via recorder_a (cluster X
    cache hot). Confirm the chain is structurally intact.
  - Construct a NEW recorder (recorder_b) — its cache is empty.
  - Have recorder_b record 1 more event.
  - Walk the chain via the new validate. If recorder_b's
    cold-start picked the lex-largest-UUID tail rather than the
    actual chain tail, validate raises.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ChainBroken,
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


async def test_fresh_recorder_cold_start_finds_chain_tail_under_tied_microseconds(
    cp_engine: Engine,
) -> None:
    """K=20 concurrent appends through recorder A with hot cache;
    then fresh recorder B records one more event; then validate.

    Without a fix to ``_last_event_for_run``, B's cold-start picks
    the lex-largest-UUID event — likely NOT the chain's actual
    tail under tied microseconds — and B's new event's
    prev_event_hash forks the chain.

    With the fix, B walks the chain links to find the unique
    tail, and the chain stays intact.
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
    recorder_a = ProvenanceRecorder(factory)

    K = 20

    async def _append_via_a(idx: int) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: recorder_a.record(
                run_id=run_id,
                event_type=ProvenanceEventType.STEP_COMPLETED,
                actor="async-test",
                payload={"i": idx},
            ),
        )

    await asyncio.wait_for(
        asyncio.gather(*[_append_via_a(i) for i in range(K)]),
        timeout=15.0,
    )

    # Sanity: chain through recorder A is intact.
    recorder_a.validate(run_id)

    # Now: simulate process-restart by constructing a fresh
    # recorder. Its ``_last_hash`` is empty for this run.
    recorder_b = ProvenanceRecorder(factory)
    recorder_b.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_COMPLETED,
        actor="restart-test",
        payload={"after": "K=20 concurrent appends"},
    )

    # Validate the chain after the cold-start write. If
    # recorder_b's cold-start picked the wrong tail, validate's
    # graph walk detects either a fork (an existing event's prev
    # equals the same hash recorder_b chose) or a partition (the
    # run's true tail's hash isn't referenced by any other
    # event, but recorder_b's new event was supposed to be that
    # successor and now isn't).
    try:
        recorder_b.validate(run_id)
    except ChainBroken as exc:
        pytest.fail(
            "BUG (cluster AG): cluster X cache produced a clean "
            "K=20 chain, but a fresh recorder's cold-start "
            "lookup of 'the latest event' picked the lex-largest "
            "UUID under tied microsecond timestamps — not the "
            "actual chain tail. The new event's prev_event_hash "
            "now points at a middle-of-chain event, forking the "
            "chain. Fix: ``_last_event_for_run`` should walk the "
            "chain by following prev_event_hash links to find "
            "the unique tail (the event whose hash is not "
            f"referenced as anyone's prev). validate raised: {exc}"
        )

    # Belt-and-suspenders: the new event must be at the END of
    # the chain (no successor). Confirm by checking no other
    # event has its prev_event_hash equal to the new event's
    # hash.
    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT event_hash, prev_event_hash FROM provenance_event "
                    "WHERE run_id = :rid"
                ),
                {"rid": str(run_id)},
            )
        )
    assert len(rows) == K + 1, (
        f"expected {K + 1} events after K=20 appends + 1 cold-start, "
        f"got {len(rows)}"
    )
    all_hashes = {h for h, _ in rows}
    referenced = {p for _, p in rows if p is not None}
    tails = all_hashes - referenced
    assert len(tails) == 1, (
        f"chain has {len(tails)} tails; exactly 1 expected. "
        "If >1 the cold-start broke the chain into multiple "
        "linear segments (likely from forking off a non-tail "
        "event)."
    )
