"""Cluster AF — recorder.validate walk-order disagrees with write order.

Cluster X fixed the *writer* with a per-instance ``_last_hash``
cache: under K=20 concurrent ``record()`` calls with tied
microsecond timestamps, each write picks the cache's hash as
``prev_event_hash`` so the chain stays linear (no forks).

But the *reader* — ``recorder.validate(run_id)`` — walks events
in ``ORDER BY timestamp ASC, id ASC``. Under tied microseconds,
the secondary sort key is ``id`` (random uuid4). The write order
the cache enforced has nothing to do with id-lex order. So
``validate()``'s walk visits events in a random permutation of
write order, and at iteration ``idx``, the expected prev pointer
(the previous walk event's hash) does NOT match the actual prev
(the previously-WRITTEN event's hash).

Concretely, with cluster X cache enforcing write order
E1→E2→…→EK and tied timestamps:
  - In DB: each Ei has prev_event_hash = hash(E(i-1)) (or None
    for E1).
  - validate walks (timestamp, id) ASC. Tied timestamps → id ASC
    permutes the events randomly relative to write order.
  - At walk position 0, validate expects prev=None, but the
    smallest-id event might have been written 5th and its
    prev = hash(E4).
  - ChainBroken raised.

The existing ``test_provenance_recorder_concurrent_appends_no_chain_break``
verifies a WEAKER property — "exactly one NULL prev across all
events" + "no fork (no two events share a prev)" — and does NOT
call validate(). So the bug is not caught.

This is the cluster-X follow-up that closes the loop: writer and
validator must agree on what "previous event" means.

Fix direction:
  A) Have validate walk in write order. We don't have a write-
     order column, but we CAN use the prev_event_hash links
     themselves: start at the genesis (prev=None), follow
     prev pointers backwards from each event to walk
     forward. O(N²) for N events but tiny N in practice.
  B) Add a per-run sequence number column (event_seq) — a
     monotonic per-run integer. validate orders by event_seq.
     Strictly correct but a schema change.
  C) Have validate match the writer's tiebreak. Since the
     writer's cache picks "the event I just committed" as prev,
     and that event's id is whatever uuid4() returned, there's
     no deterministic walk-order tiebreak that matches without
     extra metadata.

Option A is the cheapest and lock-step with the existing chain
shape. The validator follows the chain links rather than
re-deriving an order.
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


async def test_validate_walk_agrees_with_writer_under_K20_tied_microseconds(
    cp_engine: Engine,
) -> None:
    """Cluster X cache enforces write order across K=20 concurrent
    appends. validate() must accept that chain.

    Currently validate orders events by (timestamp, id) ASC. Under
    tied microsecond timestamps the id-ASC tiebreak produces a
    random permutation of write order. validate then expects the
    permuted-walk's prev pointers to chain — but the actual
    prev pointers chain in WRITE order, not walk order. So
    validate raises ChainBroken on at least one event.
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

    K = 20

    async def _append(idx: int) -> None:
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

    # Sanity: the writer's chain (cluster X cache) should be intact.
    # Check the weaker structural property the existing test asserts.
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
    assert len(rows) == K
    null_prevs = [r for r in rows if r[1] is None]
    assert len(null_prevs) == 1, (
        "writer's cache let two events claim genesis — separate bug"
    )

    # Now the cluster-AF assertion: validate must accept the chain.
    try:
        recorder.validate(run_id)
    except ChainBroken as exc:
        pytest.fail(
            "BUG (cluster AF): cluster X's cache produces a valid "
            "chain on disk (no forks, single genesis) but "
            "recorder.validate() rejects it because validate orders "
            "events by (timestamp, id) ASC and the id-ASC tiebreak "
            "permutes the write order. The walker's expected_prev "
            "doesn't match the events' actual prev pointers under "
            "tied microsecond timestamps. Fix: have validate walk "
            "the chain by following prev_event_hash links from the "
            "genesis event, instead of re-deriving an order from "
            f"timestamp+id. validate raised: {exc}"
        )
