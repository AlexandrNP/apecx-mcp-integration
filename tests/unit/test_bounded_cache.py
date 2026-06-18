"""BoundedDict — the FIFO-bounded memo cache that keeps long-lived caches from growing forever."""

from __future__ import annotations

import pytest

from apecx_integration._bounded_cache import BoundedDict


def test_evicts_oldest_when_over_cap():
    c = BoundedDict(maxsize=3)
    for i in range(5):
        c[i] = i * 10
    assert list(c.keys()) == [2, 3, 4]  # 0,1 evicted (oldest-inserted)
    assert len(c) == 3
    assert 0 not in c and 4 in c
    assert c[4] == 40


def test_drop_in_dict_semantics():
    c = BoundedDict(maxsize=2)
    c["a"] = 1
    assert "a" in c and c["a"] == 1
    c.clear()
    assert "a" not in c and len(c) == 0


def test_rewrite_existing_key_refreshes_recency_and_does_not_grow():
    c = BoundedDict(maxsize=2)
    c["a"], c["b"] = 1, 2
    c["a"] = 99  # rewrite -> moves 'a' to newest; 'b' now oldest
    c["x"] = 3  # evicts the oldest ('b'), not the just-rewritten 'a'
    assert "a" in c and c["a"] == 99
    assert "b" not in c
    assert len(c) == 2


def test_invalid_maxsize():
    with pytest.raises(ValueError):
        BoundedDict(maxsize=0)


def test_review_cache_is_bounded():
    from apecx_integration.composition.steps.taxon_candidate_review_step import (
        _REVIEW_CACHE,
        _clear_cache,
    )

    _clear_cache()
    assert isinstance(_REVIEW_CACHE, BoundedDict)
    cap = _REVIEW_CACHE._maxsize
    for i in range(cap + 50):
        _REVIEW_CACHE[f"q{i}"] = i
    assert len(_REVIEW_CACHE) == cap
    _clear_cache()
    assert len(_REVIEW_CACHE) == 0
