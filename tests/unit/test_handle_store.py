"""Unit tests for the handle store (EO-11)."""

import pytest

from apecx_integration.composition.handles.store import (
    HandleNotFound,
    HandleStore,
    default_handle_store,
)
from apecx_integration.composition.schemas.data_shapes import (
    Evidence,
    EvidenceItem,
    RecordSet,
)


def test_put_get_record_set_roundtrip():
    store = HandleStore()
    rs = RecordSet(records=[{"id": 1}, {"id": 2}], columns=["id"])
    h = store.put(rs)
    assert isinstance(h, str) and h
    got = store.get(h)
    assert isinstance(got, RecordSet)
    assert got == rs


def test_put_get_evidence_roundtrip():
    store = HandleStore()
    ev = Evidence(items=[EvidenceItem(claim="c", source="s", score=0.5)])
    got = store.get(store.put(ev))
    assert isinstance(got, Evidence)
    assert got == ev


def test_unknown_handle_raises_loudly():
    store = HandleStore()
    with pytest.raises(HandleNotFound):
        store.get("does-not-exist")


def test_delete_then_get_raises():
    store = HandleStore()
    h = store.put(RecordSet(records=[]))
    store.delete(h)
    with pytest.raises(HandleNotFound):
        store.get(h)


def test_distinct_handles_for_each_put():
    store = HandleStore()
    h1 = store.put(RecordSet(records=[]))
    h2 = store.put(RecordSet(records=[]))
    assert h1 != h2


def test_clear_empties_store():
    store = HandleStore()
    h = store.put(RecordSet(records=[]))
    store.clear()
    with pytest.raises(HandleNotFound):
        store.get(h)


def test_default_store_is_singleton():
    assert default_handle_store() is default_handle_store()


def test_inmemory_backend_is_bounded_fifo():
    """Long-lived-server leak guard: the process-lifetime backend must FIFO-evict so it
    does not grow without bound (run_workflow keeps handles for chaining, never deletes)."""
    from apecx_integration.composition.handles.store import InMemoryBackend

    b = InMemoryBackend(max_handles=3)
    handles = [b.put({"i": i}) for i in range(5)]
    # Only the 3 most-recent survive; the 2 oldest were evicted.
    assert b.get(handles[-1]) == {"i": 4}
    assert b.get(handles[2]) == {"i": 2}
    for old in handles[:2]:
        with pytest.raises(HandleNotFound):
            b.get(old)


def test_inmemory_backend_rejects_bad_cap():
    from apecx_integration.composition.handles.store import InMemoryBackend

    with pytest.raises(ValueError):
        InMemoryBackend(max_handles=0)
