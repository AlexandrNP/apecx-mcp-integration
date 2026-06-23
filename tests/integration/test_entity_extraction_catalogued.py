"""The composer catalog MUST include EntityExtractionStep — the reviewer prompt's
canonical "extract entities" example. Without it the spec expander rejects the
LLM's (prompt-following) choice with "no catalog match", making real-LLM compose
fragile (root-caused 2026-06-23: test_t01_ac1_against_ollama flaked on exactly
this). Real catalog + real wrapper load, no mocks.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import apecx_integration
from apecx_integration.composition.component_catalog import ComponentCatalog
from apecx_integration.composition.steps.db_integration_wrappers import EntityExtractionStep

_CFGDIR = Path(apecx_integration.__file__).parent / "composition"


def _catalog() -> ComponentCatalog:
    cfg = yaml.safe_load((_CFGDIR / "composer_config.yml").read_text())
    return ComponentCatalog.from_manifests(
        [(_CFGDIR / p).resolve() for p in cfg["component_catalog_paths"]]
    )


def test_entity_extraction_is_catalogued():
    names = {c.class_path.rsplit(".", 1)[-1] for c in _catalog().components}
    assert "EntityExtractionStep" in names, (
        "EntityExtractionStep dropped from the composer catalog — the reviewer "
        "prompt cites it as the canonical example; without it real-LLM compose "
        "fails with 'no catalog match'."
    )


def test_entity_extraction_wrapper_loads_via_from_config():
    step = EntityExtractionStep.from_config(
        str(_CFGDIR / "_catalog_steps" / "entity_extraction.yml")
    )
    assert step is not None
    assert step.name == "entity_extraction"
