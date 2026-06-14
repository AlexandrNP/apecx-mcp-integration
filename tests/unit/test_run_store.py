"""Unit tests for the session RunStore (EO-04), focused on the bounded-memory guard."""

import pytest

from apecx_integration.composition.runtime.provenance_wiring import RunSummary
from apecx_integration.composition.runtime.run_store import RunStore


def _record(store: RunStore, name: str = "wf"):
    return store.record(
        workflow_name=name,
        status="ok",
        run_summary=RunSummary(workflow_status="ok", steps=[]),
        workflow_result=None,
    )


def test_record_and_get_roundtrip():
    store = RunStore()
    rec = _record(store)
    assert store.get(rec.run_id) is rec
    assert store.get("nonexistent") is None  # loud None, never a fabricated record


def test_store_is_bounded_fifo():
    """Long-lived-server leak guard: each record holds the full WorkflowResult (~12-15KB);
    the process-lifetime singleton must FIFO-evict so memory does not grow without bound."""
    store = RunStore(max_runs=3)
    recs = [_record(store) for _ in range(5)]
    # Only the 3 most-recent runs survive.
    assert len(store.session_runs()) == 3
    survivors = {r.run_id for r in store.session_runs()}
    assert survivors == {recs[2].run_id, recs[3].run_id, recs[4].run_id}
    # The 2 oldest were evicted — get() returns None (caller surfaces "unknown run_id"),
    # NOT a silent wrong record.
    assert store.get(recs[0].run_id) is None
    assert store.get(recs[1].run_id) is None
    # The newest is intact and the monotonic order counter keeps counting past eviction.
    assert store.get(recs[4].run_id).order == 5


def test_rejects_bad_cap():
    with pytest.raises(ValueError):
        RunStore(max_runs=0)
