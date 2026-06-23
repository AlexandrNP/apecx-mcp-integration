"""WS1a: the composer's FAISS-index guard.

The guard DEGRADES LOUD to linear-scan when the semantic index is missing or
STALE (never crashes — the previous fail-fast is intentionally replaced — and
never silently retrieves over an outdated corpus), and uses the index when it is
present + fresh. Builds a REAL ComponentIndex over the REAL composer manifests
(no mocks); config overrides use the real pydantic ComposerConfig.model_copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.lightweight.component_index import ComponentIndex

import apecx_integration
from apecx_integration.composition.composer import Composer

_CONFIG = Path(apecx_integration.__file__).parent / "composition" / "composer_config.yml"


@pytest.fixture(scope="module")
def composer() -> Composer:
    return Composer.from_config(str(_CONFIG))


@pytest.fixture(scope="module")
def fresh_index_dir(composer, tmp_path_factory) -> Path:
    """A real FAISS index built over the composer's CURRENT manifest corpus."""
    d = tmp_path_factory.mktemp("rag_index")
    idx = ComponentIndex()
    idx.rebuild(
        manifest_paths=list(composer._config.component_catalog_paths),
        library_version=composer._config.library_version,
    )
    idx.save(d)
    return d


def test_missing_index_degrades_to_linear(composer, tmp_path):
    cfg = composer._config.model_copy(update={"rag_index_dir": str(tmp_path / "nope")})
    assert composer._load_rag_index_or_degrade(cfg) is None


def test_corrupt_index_degrades_to_linear(composer, tmp_path):
    # Files exist but are garbage => ComponentIndex.load raises => degrade loud
    # (must not crash composer init).
    d = tmp_path / "corrupt"
    d.mkdir()
    (d / "faiss.bin").write_bytes(b"not a real faiss index")
    (d / "metadata.json").write_text("{ this is not valid json", encoding="utf-8")
    cfg = composer._config.model_copy(update={"rag_index_dir": str(d)})
    assert composer._load_rag_index_or_degrade(cfg) is None


def test_fresh_index_enables_semantic_retrieval(composer, fresh_index_dir):
    cfg = composer._config.model_copy(update={"rag_index_dir": str(fresh_index_dir)})
    idx = composer._load_rag_index_or_degrade(cfg)
    assert idx is not None, "a fresh index over the current corpus must be used"
    # Semantic retrieval surfaces the TDR pattern for a paraphrased query.
    hits = idx.search("a loop that keeps fixing code until the tests stop failing", k=5)
    assert any("tdr" in h.id.lower() for h in hits), [h.id for h in hits]


def test_stale_index_degrades_to_linear(composer, fresh_index_dir):
    # The index was built over the FULL corpus; point the config's corpus at a
    # SUBSET so the index no longer matches => stale => degrade loud (not crash,
    # not stale-serve).
    full = list(composer._config.component_catalog_paths)
    subset = full[:1]
    assert subset != full, "need >1 manifest for a meaningful stale test"
    cfg = composer._config.model_copy(
        update={
            "rag_index_dir": str(fresh_index_dir),
            "component_catalog_paths": subset,
        }
    )
    assert composer._load_rag_index_or_degrade(cfg) is None
