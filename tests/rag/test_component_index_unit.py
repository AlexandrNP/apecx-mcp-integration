"""T03 AC1 + AC2 + AC4 unit tests for
``nanobrain.lightweight.component_index.ComponentIndex``.

- **AC1**: ``rebuild()`` runs in <60s on the current library, and the
  ``index_hash`` is deterministic given the same library version + same
  component set.
- **AC2**: ``search(...)`` returns ``ComponentMatch`` instances whose
  ``similarity`` is in [0.0, 1.0].
- **AC4**: the index storage path is configurable (``save()`` /
  ``load()``) and the index is regenerable from scratch — a reloaded
  index returns the same hits as the freshly-rebuilt one.

Cost note — model loading dominates (~5s on cold cache, <1s warm per
encode). To amortize we use a module-scoped fixture for the built
index. AC1's <60s budget still has plenty of headroom on a 10-component
corpus; on a much larger corpus the budget would need revisiting (see
the test docstring for a failure mode to watch).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

try:
    from nanobrain.lightweight.component_index import (
        ComponentIndex,
        ComponentMatch,
    )

    _NB_IMPORT_ERROR: str | None = None
except ImportError as exc:
    ComponentIndex = None  # type: ignore[assignment]
    ComponentMatch = None  # type: ignore[assignment]
    _NB_IMPORT_ERROR = str(exc)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
# Any non-empty component manifest works here — these AC1/AC2/AC4 tests
# exercise ComponentIndex mechanics (rebuild budget, deterministic hash,
# save/load, similarity bounds), not workflow-specific recall.
# (violin_bvbrc retired 2026-06-15; rag_e2e_synthesis survives.)
MANIFEST = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "rag_e2e_synthesis"
    / "manifest.yml"
)


def _model_cache_present() -> bool:
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    return (cache / "models--sentence-transformers--all-mpnet-base-v2").is_dir()


SKIP_NB_MISSING = f"nanobrain.lightweight.component_index not importable: {_NB_IMPORT_ERROR}"
SKIP_MODEL_MISSING = "all-mpnet-base-v2 not cached — cold CI"


pytestmark = [
    pytestmark,
    pytest.mark.skipif(ComponentIndex is None, reason=SKIP_NB_MISSING),
    pytest.mark.skipif(not _model_cache_present(), reason=SKIP_MODEL_MISSING),
]


@pytest.fixture(scope="module")
def built_index():
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    return idx


# ---------------------------------------------------------------------------
# AC1 — rebuild budget + deterministic hash
# ---------------------------------------------------------------------------


def test_ac1_rebuild_under_60_seconds():
    """Wall-time floor. If this breaches 60s, either the corpus has
    grown past what a flat IP index can handle in one pass (switch to
    IVF?) OR the model is being loaded twice OR the embedding batch
    size collapsed. All three are distinct root causes."""
    idx = ComponentIndex()
    t0 = time.monotonic()
    idx.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    elapsed = time.monotonic() - t0
    assert elapsed < 60.0, f"rebuild took {elapsed:.1f}s (>60s budget)"
    assert len(idx) > 0


def test_ac1_index_hash_is_deterministic_across_rebuilds():
    """Two rebuilds, same inputs, identical hash."""
    a = ComponentIndex()
    a.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    b = ComponentIndex()
    b.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    assert a.index_hash == b.index_hash
    assert len(a.index_hash) == 64  # sha256 hex


def test_ac1_index_hash_changes_when_library_version_changes():
    a = ComponentIndex()
    a.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    b = ComponentIndex()
    b.rebuild(manifest_paths=[MANIFEST], library_version="0.2.0-test")
    assert a.index_hash != b.index_hash


def test_ac1_index_hash_changes_when_model_changes():
    """Pretend we rebuilt with a different model — hash must reflect
    that so stale-index detection works."""
    a = ComponentIndex(model_name="model-A")
    a._records = ()
    b = ComponentIndex(model_name="model-B")
    b._records = ()
    # Call the static hash helper directly rather than rebuild() —
    # we want the hash to change on model name alone, independent of
    # whether the two models' output tensors differ.
    ha = ComponentIndex._compute_hash(records=(), library_version="v", model_name="model-A")
    hb = ComponentIndex._compute_hash(records=(), library_version="v", model_name="model-B")
    assert ha != hb


# ---------------------------------------------------------------------------
# AC2 — similarity range + record shape
# ---------------------------------------------------------------------------


def test_ac2_search_returns_component_match_instances(built_index):
    hits = built_index.search("anything at all", k=3)
    assert all(isinstance(h, ComponentMatch) for h in hits)
    assert 0 < len(hits) <= 3


def test_ac2_similarity_is_bounded_to_unit_interval(built_index):
    hits = built_index.search("any query works here", k=len(built_index))
    for h in hits:
        assert 0.0 <= h.similarity <= 1.0, f"similarity {h.similarity} out of [0, 1] for {h.id}"


def test_ac2_similarity_is_sorted_descending(built_index):
    hits = built_index.search("annotate genome hits with proteins", k=5)
    sims = [h.similarity for h in hits]
    assert sims == sorted(sims, reverse=True)


def test_ac2_empty_query_returns_empty_list(built_index):
    assert built_index.search("") == []
    assert built_index.search("   \n  \t") == []


def test_ac2_k_larger_than_corpus_does_not_raise(built_index):
    hits = built_index.search("genomic annotation pipeline", k=9999)
    assert len(hits) == len(built_index)


# ---------------------------------------------------------------------------
# AC4 — configurable path + regenerable from scratch
# ---------------------------------------------------------------------------


def test_ac4_save_and_load_roundtrip_preserves_hash(tmp_path):
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    save_dir = tmp_path / "rag_index"
    idx.save(save_dir)

    assert (save_dir / "faiss.bin").is_file()
    assert (save_dir / "metadata.json").is_file()

    reloaded = ComponentIndex.load(save_dir)
    assert reloaded.index_hash == idx.index_hash
    assert len(reloaded) == len(idx)


def test_ac4_reloaded_index_returns_same_top_hits(tmp_path, built_index):
    save_dir = tmp_path / "rag_index"
    built_index.save(save_dir)
    reloaded = ComponentIndex.load(save_dir)

    q = "enrich pathogen records with VIOLIN vaccine info"
    a = [h.id for h in built_index.search(q, k=5)]
    b = [h.id for h in reloaded.search(q, k=5)]
    assert a == b, (
        f"reloaded index produced different top-5 order.\n  fresh:    {a}\n  reloaded: {b}"
    )


def test_ac4_save_requires_rebuild(tmp_path):
    fresh = ComponentIndex()
    with pytest.raises(RuntimeError, match="rebuild"):
        fresh.save(tmp_path / "nope")


def test_ac4_search_requires_rebuild_or_load():
    fresh = ComponentIndex()
    with pytest.raises(RuntimeError, match="rebuild|load"):
        fresh.search("query", k=1)


def test_ac4_rebuild_rejects_empty_corpus(tmp_path):
    empty_manifest = tmp_path / "empty.yml"
    empty_manifest.write_text("workflow:\n  name: empty\ncomponents: []\n")
    idx = ComponentIndex()
    with pytest.raises(ValueError, match="empty corpus"):
        idx.rebuild(manifest_paths=[empty_manifest], library_version="v")
