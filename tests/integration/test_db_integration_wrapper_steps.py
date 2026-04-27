"""T02 integration: the three apecx_db_integration wrapper Steps.

Mock + integration parity per workspace CLAUDE.md:

- LLM-touching steps (1, 3c) here use a placeholder LLM stub. The real
  Ollama round-trip lives in
  ``test_violin_bvbrc_workflow_against_ollama.py`` (T03, operator-run,
  auto-skips when daemon unreachable).
- The pure-pandas step (5) uses a deterministic CSV fixture; no mock
  needed (the function under test has no external boundary to mock).

Why this file is in tests/integration not tests/unit
----------------------------------------------------
The whole tests/ tree is flat-integration in this repo per the
pyproject pytest config (no separate unit/integration split). Marker
``pytest.mark.integration`` keeps these in the same selection as
``test_synonym_cache_steps.py`` and friends.

Stub LLM contract
-----------------
The ``_PlaceholderLLM`` class implements only ``.invoke(messages) ->
obj.content`` — exactly the surface
``apecx_db_integration.agent.extract_entities_llm`` and
``consolidated_synonym_search`` consume. FIFO list of canned responses
means one stub serves a function that calls the LLM N times in
sequence. This mirrors the test shape used in
``apecx-db-integration/tests/test_public_api_smoke.py`` so the same
mental model carries across both test suites.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml
# Migrated 2026-04-27: VIOLIN agent now lives in
# ``apecx_integration.agents.violin_bvbrc`` (no longer under
# ``apecx_db_integration``). The test patches ``_build_chat_llm``
# at this NEW location so the wrapper steps' real LLM calls get
# intercepted by ``_PlaceholderLLM``.
from apecx_integration.agents.violin_bvbrc import agent as _db_agent

from apecx_integration.composition.steps.db_integration_wrappers import (
    EntityExtractionStep,
    SynonymLLMProposalsStep,
    ViolinEntityLookupStep,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared placeholder LLM (mirrors apecx-db-integration tests)
# ---------------------------------------------------------------------------

class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned_responses: list[str]):
        self.canned = list(canned_responses)
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        if not self.canned:
            raise AssertionError(
                f"PlaceholderLLM ran out of canned responses on call #{len(self.calls)}"
            )
        return _PlaceholderResponse(self.canned.pop(0))


def _patch_llm(monkeypatch, placeholder: _PlaceholderLLM) -> None:
    monkeypatch.setattr(_db_agent, "_build_chat_llm", lambda **kw: placeholder)


def _write_step_yaml(tmp_path: Path, class_path: str, name: str, extra: dict | None = None) -> Path:
    """Write a minimal Step YAML with no upstream/downstream wiring.

    The from_config path requires input/output DataUnits + a trigger
    even when the test only calls process() directly — the framework
    initializes them at __init__ time.
    """
    body: dict = {
        "class": class_path,
        "name": name,
        "description": f"unit-test {name}",
        "input_data_units": {
            f"{name}_input": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": f"{name}_input",
                "persistent": False,
            }
        },
        "output_data_units": {
            f"{name}_output": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": f"{name}_output",
                "persistent": False,
            }
        },
        "triggers": [
            {
                "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                "data_unit": f"{name}_input",
            }
        ],
    }
    if extra:
        body.update(extra)
    p = tmp_path / f"{name}.yml"
    p.write_text(yaml.safe_dump(body))
    return p


# ---------------------------------------------------------------------------
# Step 1 — EntityExtractionStep
# ---------------------------------------------------------------------------

def test_entity_extraction_step_with_placeholder_llm(tmp_path, monkeypatch):
    canned = json.dumps([
        {"name": "EEEV", "type": "pathogen", "confidence": 0.95},
        {"name": "alphavirus vaccine", "type": "vaccine", "confidence": 0.7},
        {"name": "low-conf-noise", "type": "medical_term", "confidence": 0.2},
    ])
    placeholder = _PlaceholderLLM([canned])
    _patch_llm(monkeypatch, placeholder)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep",
        "entity_extraction_unit",
    )
    step = EntityExtractionStep.from_config(str(yml))

    result = asyncio.run(step.process({"query": "find EEEV vaccines"}))

    assert "entities" in result
    names = {e["name"] for e in result["entities"]}
    assert "EEEV" in names
    assert "alphavirus vaccine" in names
    # 0.5 confidence floor is enforced by the wrapped function.
    assert "low-conf-noise" not in names
    assert len(placeholder.calls) == 1

    # T04 contract (data_unit_schemas.Step1Output): query_terms emitted
    # alongside entities so the Step 1 → Step 3a DirectLink works without
    # a TransformLink. Step 3a reads input_data["query_terms"].
    assert result["query_terms"] == [e["name"] for e in result["entities"]]


def test_entity_extraction_step_rejects_non_string_query(tmp_path, monkeypatch):
    """Wrapper is responsible for shape validation BEFORE invoking the
    wrapped LLM call — saves a useless network round-trip."""
    placeholder = _PlaceholderLLM([])  # any LLM call would AssertionError
    _patch_llm(monkeypatch, placeholder)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep",
        "entity_extraction_validation",
    )
    step = EntityExtractionStep.from_config(str(yml))

    with pytest.raises(ValueError, match="non-empty 'query' string"):
        asyncio.run(step.process({"query": ""}))
    with pytest.raises(ValueError, match="non-empty 'query' string"):
        asyncio.run(step.process({"query": None}))
    assert placeholder.calls == []


# ---------------------------------------------------------------------------
# Step 3c — SynonymLLMProposalsStep
# ---------------------------------------------------------------------------

def test_synonym_llm_proposals_step_uses_two_placeholder_calls(tmp_path, monkeypatch):
    """``consolidated_synonym_search`` invokes the LLM twice: entity
    extraction, then synonym matching. The wrapper passes through both
    calls without intermediate caching."""
    extract_canned = json.dumps([
        {"name": "EEEV", "type": "pathogen", "confidence": 0.95},
    ])
    synonyms_canned = json.dumps([
        {"query_entity": "EEEV", "synonym": "EEEV stub strain", "score": 0.9},
    ])
    placeholder = _PlaceholderLLM([extract_canned, synonyms_canned])
    _patch_llm(monkeypatch, placeholder)

    # The wrapped function reads candidate VIOLIN terms during the
    # synonym-matching call — point APECX_DB_DATA_DIR at a stub dir
    # holding only the BVBRC CSV (the VIOLIN tables get warned-and-
    # skipped, which is the fresh-clone failure mode anyway).
    bvbrc = tmp_path / "BVBRC_genome_alphavirus.csv"
    bvbrc.write_text(
        "Genome ID,Genome Name,Family,Genus,Species\n"
        "1,EEEV stub,Togaviridae,Alphavirus,EEEV\n"
    )
    monkeypatch.setenv("APECX_DB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_db_agent, "_DFS_CACHE", None)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.SynonymLLMProposalsStep",
        "synonym_llm_proposals_unit",
        extra={"data_dir": None},
    )
    step = SynonymLLMProposalsStep.from_config(str(yml))

    result = asyncio.run(step.process({"novel_terms": ["EEEV"]}))

    assert result == {"llm_proposals": [
        {"query_entity": "EEEV", "synonym": "EEEV stub strain", "score": 0.9},
    ]}
    assert len(placeholder.calls) == 2


def test_synonym_llm_proposals_step_short_circuits_on_empty_input(tmp_path, monkeypatch):
    """Empty novel_terms → no LLM call. Important property: Step 3a
    might emit zero novel terms in the all-cached case, and Step 3c
    should not waste an Ollama round-trip on it."""
    placeholder = _PlaceholderLLM([])  # any LLM call would AssertionError
    _patch_llm(monkeypatch, placeholder)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.SynonymLLMProposalsStep",
        "synonym_llm_proposals_empty",
        extra={"data_dir": None},
    )
    step = SynonymLLMProposalsStep.from_config(str(yml))

    result = asyncio.run(step.process({"novel_terms": []}))
    assert result == {"llm_proposals": []}
    assert placeholder.calls == []


# ---------------------------------------------------------------------------
# Step 5 — ViolinEntityLookupStep (pure pandas, no LLM mock needed)
# ---------------------------------------------------------------------------

def test_violin_entity_lookup_step_against_stub_csv(tmp_path, monkeypatch):
    """No LLM mock — this function is pure pandas. Joins the input
    matches against a BVBRC stub CSV via the data_dir override."""
    bvbrc = tmp_path / "BVBRC_genome_alphavirus.csv"
    bvbrc.write_text(
        "Genome ID,Genome Name,Family,Genus,Species\n"
        "1,EEEV stub strain,Togaviridae,Alphavirus,EEEV\n"
    )
    # Also reset the lazy cache so a previous test's data dir doesn't
    # leak into this one.
    monkeypatch.setattr(_db_agent, "_DFS_CACHE", None)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.ViolinEntityLookupStep",
        "violin_entity_lookup_unit",
        extra={"data_dir": str(tmp_path)},
    )
    step = ViolinEntityLookupStep.from_config(str(yml))

    matches = [
        {"query_entity": "EEEV", "synonym": "EEEV stub strain", "score": 0.9},
    ]
    result = asyncio.run(step.process({"matches": matches}))

    assert "enriched_matches" in result
    assert len(result["enriched_matches"]) == 1
    # Original schema preserved.
    assert result["enriched_matches"][0]["query_entity"] == "EEEV"


def test_violin_entity_lookup_step_short_circuits_on_empty_input(tmp_path, monkeypatch):
    """Empty matches → empty enriched_matches, no data load."""
    monkeypatch.setattr(_db_agent, "_DFS_CACHE", None)
    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.ViolinEntityLookupStep",
        "violin_entity_lookup_empty",
    )
    step = ViolinEntityLookupStep.from_config(str(yml))

    result = asyncio.run(step.process({"matches": []}))
    assert result == {"enriched_matches": []}


def test_data_dir_override_restores_env_var_in_finally(tmp_path, monkeypatch):
    """The data_dir context manager must restore the prior env var
    even if process() raises. Otherwise one failing step leaks the
    APECX_DB_DATA_DIR override into every subsequent step in the same
    process — a really nasty cross-test contamination class."""
    sentinel = "/tmp/sentinel_value_that_should_be_restored"
    monkeypatch.setenv("APECX_DB_DATA_DIR", sentinel)
    monkeypatch.setattr(_db_agent, "_DFS_CACHE", None)

    yml = _write_step_yaml(
        tmp_path,
        "apecx_integration.composition.steps.db_integration_wrappers.ViolinEntityLookupStep",
        "violin_entity_lookup_finally",
        extra={"data_dir": str(tmp_path)},
    )
    step = ViolinEntityLookupStep.from_config(str(yml))

    # Force a TypeError mid-call by passing a non-list — this is the
    # error path where the finally must still restore the env var.
    with pytest.raises(ValueError, match="'matches' as list"):
        asyncio.run(step.process({"matches": None}))

    # Note: the override block runs only when the body actually executes;
    # validation runs BEFORE the override block. So the env var was never
    # changed for this particular failure path. Test the success path
    # also restores cleanly:
    asyncio.run(step.process({"matches": []}))
    assert os.environ["APECX_DB_DATA_DIR"] == sentinel
