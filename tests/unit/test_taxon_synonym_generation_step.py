"""Unit tests for TaxonSynonymGenerationStep — LLM candidate-name generation (fallback step 1).

The LLM is mocked by monkeypatching ``build_chat_llm`` / ``preflight_llm_model`` ON THE STEP'S
MODULE (no unittest.mock). Real-LLM parity for this fallback lives in the integration suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import apecx_integration.composition.steps.taxon_synonym_generation_step as mod
from apecx_integration.composition.steps.taxon_synonym_generation_step import (
    TaxonSynonymGenerationStep,
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages):  # noqa: D401 - mirrors langchain ChatModel.invoke
        return _Resp(self._content)


def _boom(*a, **k):
    pytest.fail("LLM path must not be reached")


def _no_llm(*a, **k):
    raise RuntimeError("no LLM reachable")


def _stage(tmp_path: Path) -> TaxonSynonymGenerationStep:
    p = tmp_path / "synonym_gen.yml"
    p.write_text("name: synonym_gen_test\n")
    return TaxonSynonymGenerationStep.from_config(str(p))


def test_from_config_constructs(tmp_path):
    step = _stage(tmp_path)
    assert step.COMPONENT_TYPE == "taxon_synonym_generation_step"
    assert step.name == "synonym_gen_test"


def test_short_circuits_when_already_resolved(tmp_path, monkeypatch):
    """canonical_iri carrying NCBITaxon -> the dict resolver already won; no LLM, no preflight."""
    monkeypatch.setattr(mod, "build_chat_llm", _boom)
    monkeypatch.setattr(mod, "preflight_llm_model", _boom)
    bundle = {
        "query": "chikv",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
    }
    out = asyncio.run(_stage(tmp_path).process({"synonym_gen_input": bundle}))
    assert "taxon_synonyms" not in out
    assert out["canonical_iri"].endswith("NCBITaxon_37124")


def test_degrades_to_seeds_when_no_llm(tmp_path, monkeypatch):
    """preflight raising (no LLM) -> the deterministically-extracted seed names + a NAMED note."""
    monkeypatch.setattr(mod, "preflight_llm_model", _no_llm)
    monkeypatch.setattr(mod, "build_chat_llm", _boom)  # must NOT be reached after preflight fails
    out = asyncio.run(
        _stage(tmp_path).process({"synonym_gen_input": {"query": "chikungunya virus E1"}})
    )
    assert "Chikungunya virus" in out["taxon_synonyms"]
    assert out["taxon_synonym_note"] == "LLM unavailable; using extracted names only"


def test_llm_success_parses_merges_and_dedups(tmp_path, monkeypatch):
    """Bullets/numbering stripped, merged with seeds, de-duped case-insensitively."""
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(
        mod,
        "build_chat_llm",
        lambda **k: _FakeLLM("- Chikungunya virus\n* CHIKV\n1. Alphavirus chikungunyae\n\n"),
    )
    out = asyncio.run(_stage(tmp_path).process({"synonym_gen_input": {"query": "chikungunya"}}))
    syns = out["taxon_synonyms"]
    assert "chikungunya" in [s.lower() for s in syns]  # seed
    assert "CHIKV" in syns  # bullet stripped
    assert "Alphavirus chikungunyae" in syns  # numbering stripped
    # "Chikungunya virus" appears as both a seed and an LLM line -> de-duped (one occurrence).
    assert sum(1 for s in syns if s.lower() == "chikungunya virus") == 1
    assert "taxon_synonym_note" not in out


def test_caps_synonym_list_at_eight(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    many = "\n".join(f"Name{i}" for i in range(20))
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM(many))
    out = asyncio.run(_stage(tmp_path).process({"synonym_gen_input": {"query": "chikungunya"}}))
    assert len(out["taxon_synonyms"]) == 8


def test_llm_error_degrades_to_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)

    def _raise(**k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "build_chat_llm", _raise)
    out = asyncio.run(_stage(tmp_path).process({"synonym_gen_input": {"query": "chikungunya"}}))
    assert "Chikungunya virus" in out["taxon_synonyms"]
    assert "LLM synonym generation failed" in out["taxon_synonym_note"]


def test_non_dict_input_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process("not a dict"))
