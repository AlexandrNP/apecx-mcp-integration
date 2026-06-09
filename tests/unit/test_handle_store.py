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
