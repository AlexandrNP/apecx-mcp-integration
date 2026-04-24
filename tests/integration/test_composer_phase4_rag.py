"""T-COMP Phase 4 — RAG retrieval swap-in.

Exit criterion: when ``composer_config.rag_index_dir`` points at a
ComponentIndex artifact produced by ``scripts/build_rag_index.py``,
``Composer._retrieve`` uses FAISS + mpnet embeddings instead of the
Phase-2 linear-scan ``ComponentCatalog.search``. When the field is
unset, the composer falls back to the Phase-2 path unchanged
(regression-guarded here so a future refactor can't silently strand
the fallback branch).

Cost
----
Building the index costs one model load (~5s cold, ~1s on cached
model + pre-compiled sentence_transformers). We amortize via a
``module``-scoped fixture; individual tests add negligible time.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml

from apecx_integration.composition.composer import (
    Composer,
    ComposerConfigurationError,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)


def _nanobrain_importable() -> bool:
    try:
        import nanobrain.lightweight.component_index  # noqa: F401
    except ImportError:
        return False
    return True


def _model_cache_present() -> bool:
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    return (
        cache / "models--sentence-transformers--all-mpnet-base-v2"
    ).is_dir()


pytestmark = [
    pytestmark,
    pytest.mark.skipif(
        not _nanobrain_importable(),
        reason="nanobrain.lightweight.component_index not importable",
    ),
    pytest.mark.skipif(
        not _model_cache_present(),
        reason="all-mpnet-base-v2 not cached — cold CI",
    ),
]


# ---------------------------------------------------------------------------
# Placeholder LLM — compose() exercised without Ollama
# ---------------------------------------------------------------------------


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _PlaceholderResponse(self.canned)


def _make_llm_factory(canned: str):
    captured: list[_PlaceholderLLM] = []

    def _factory(**_kwargs):
        llm = _PlaceholderLLM(canned)
        captured.append(llm)
        return llm

    return captured, _factory


HAPPY_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: phase4_smoke
    description: "smoke test"
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rag_index_dir(tmp_path_factory):
    """Build the FAISS index once per test module."""
    from scripts.build_rag_index import build

    out = tmp_path_factory.mktemp("rag_index")
    built = build(DEFAULT_CONFIG.resolve(), out)
    return built


@pytest.fixture
def config_with_rag(tmp_path, rag_index_dir):
    """A composer config YAML that points rag_index_dir at our fixture."""
    source = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    # Resolve relative paths against the original config's parent so
    # the copied config still sees them.
    for key in ("component_catalog_paths",):
        raw = source.get(key) or []
        source[key] = [
            str((DEFAULT_CONFIG.parent / p).resolve()) for p in raw
        ]
    if source.get("sandbox_whitelist_path"):
        source["sandbox_whitelist_path"] = str(
            (DEFAULT_CONFIG.parent / source["sandbox_whitelist_path"]).resolve()
        )
    if source.get("prompt_dir"):
        source["prompt_dir"] = str(
            (DEFAULT_CONFIG.parent / source["prompt_dir"]).resolve()
        )
    source["rag_index_dir"] = str(rag_index_dir)
    target = tmp_path / "composer_config_rag.yml"
    target.write_text(yaml.safe_dump(source), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Tests — RAG path
# ---------------------------------------------------------------------------


def test_composer_loads_rag_index_when_configured(config_with_rag):
    composer = Composer.from_config(config_with_rag)
    assert composer._rag_index is not None
    assert len(composer._rag_index) >= 9


def test_retrieve_uses_rag_not_linear_scan(config_with_rag):
    """RAG and linear-scan can disagree on ordering for semantically-
    phrased queries — RAG is supposed to win on these. The query below
    has zero word-overlap with descriptions containing the word
    "pathogen" / "virus" / "genome", so linear-scan should whiff
    while RAG lifts the right component to the top."""
    composer = Composer.from_config(config_with_rag)
    hits = composer._retrieve(
        "identify microbial agents in a free-text biomedical inquiry",
        k=3,
    )
    assert hits, "RAG returned no hits"
    names = [h.component.name for h in hits]
    assert "entity_extraction" in names, (
        f"expected entity_extraction in top-3 RAG hits; got {names}"
    )


def test_compose_end_to_end_through_rag_retrieval(config_with_rag):
    captured, factory = _make_llm_factory(HAPPY_RESPONSE)
    composer = Composer.from_config(config_with_rag)
    composer._llm_factory = factory
    result = asyncio.run(composer.compose("extract viral entity names"))
    assert result.retrieved_components, "no components retrieved via RAG"
    assert len(captured) == 1
    # The placeholder LLM saw the real candidate-block — sanity-check
    # that the rendered candidates mention a library component by name.
    user_msg = captured[0].calls[0][1].content
    assert "entity_extraction" in user_msg


# ---------------------------------------------------------------------------
# Tests — fallback + error paths
# ---------------------------------------------------------------------------


def test_composer_without_rag_index_falls_back_to_linear_scan():
    """No ``rag_index_dir`` → Phase-2 linear-scan remains default."""
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert composer._rag_index is None
    hits = composer._retrieve("biomedical entities", k=3)
    # Falls back to ComponentCatalog.search, which returns SearchHit.
    assert hits or len(composer._catalog) == 0


def test_composer_rejects_missing_rag_index_dir(tmp_path):
    """``rag_index_dir`` set but pointing at nothing → clean error."""
    source = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    source["prompt_dir"] = str(
        (DEFAULT_CONFIG.parent / source["prompt_dir"]).resolve()
    )
    source["component_catalog_paths"] = []
    source.pop("sandbox_whitelist_path", None)
    source["rag_index_dir"] = str(tmp_path / "does_not_exist")
    cfg = tmp_path / "bad_rag.yml"
    cfg.write_text(yaml.safe_dump(source), encoding="utf-8")

    with pytest.raises(ComposerConfigurationError, match="missing"):
        Composer.from_config(cfg)


def test_composer_rejects_rag_dir_missing_metadata(tmp_path):
    """Directory exists but lacks ``metadata.json`` → error."""
    dir_ = tmp_path / "half_built"
    dir_.mkdir()
    (dir_ / "faiss.bin").write_bytes(b"stub")

    source = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    source["prompt_dir"] = str(
        (DEFAULT_CONFIG.parent / source["prompt_dir"]).resolve()
    )
    source["component_catalog_paths"] = []
    source.pop("sandbox_whitelist_path", None)
    source["rag_index_dir"] = str(dir_)
    cfg = tmp_path / "half_built.yml"
    cfg.write_text(yaml.safe_dump(source), encoding="utf-8")

    with pytest.raises(ComposerConfigurationError, match="missing"):
        Composer.from_config(cfg)
