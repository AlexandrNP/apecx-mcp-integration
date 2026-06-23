"""WS1b step 2: the curated reusable reasoning patterns are in the composer's
RETRIEVAL corpus (via the real composer_config.yml), are retrievable by a
relevant query, and every advertised pattern component actually instantiates.

Builds the catalog from the REAL composer_config.yml (not a hand-made path list),
so it verifies the config wiring took effect — and asserts each pattern component's
wrapper loads via its declared class, closing the "advertise a component the
composer can't instantiate" silent gap. Real files, deterministic, no Ollama.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import apecx_integration
from apecx_integration.composition.component_catalog import ComponentCatalog
from apecx_integration.composition.steps.reasoning_pattern_step import (
    ReasoningPatternStep,
)

_CONFIG = Path(apecx_integration.__file__).parent / "composition" / "composer_config.yml"
_PATTERN_IDS = ("tdr_loop_pattern", "best_of_n_pattern", "rag_e2e_synthesis_pattern")


def _catalog_from_real_config() -> ComponentCatalog:
    cfg = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    paths = [(_CONFIG.parent / p).resolve() for p in cfg["component_catalog_paths"]]
    return ComponentCatalog.from_manifests(paths)


def test_patterns_present_in_real_composer_corpus():
    cat = _catalog_from_real_config()
    ids = [c.id for c in cat.components]
    for pat in _PATTERN_IDS:
        assert any(pat in cid for cid in ids), f"{pat} missing from corpus: {ids}"


def test_retrieval_surfaces_a_pattern_for_relevant_query():
    cat = _catalog_from_real_config()
    hits = cat.search("iteratively refine code until the tests pass", k=10)
    hit_ids = [h.component.id for h in hits]
    assert any("tdr_loop_pattern" in i for i in hit_ids), (
        f"tdr_loop pattern not retrieved for a TDR-shaped query: {hit_ids}"
    )


def test_every_pattern_component_actually_instantiates():
    # The catalog must never advertise a component whose wrapper can't load —
    # that would be a broken reference the composer emits, failing only at
    # compose/load time. Verify each here, up front.
    cat = _catalog_from_real_config()
    pats = [c for c in cat.components if c.domain == "reasoning_pattern"]
    assert len(pats) == len(_PATTERN_IDS), f"expected {len(_PATTERN_IDS)} patterns, got {len(pats)}"
    for c in pats:
        assert c.class_path.endswith("ReasoningPatternStep"), c.class_path
        assert c.yaml_path_absolute and Path(c.yaml_path_absolute).is_file(), c.id
        step = ReasoningPatternStep.from_config(c.yaml_path_absolute)
        assert step.inner_workflow is not None, c.id
