"""T02 integration: the EntityExtractionStep apecx_db_integration wrapper.

Mock + integration parity per workspace CLAUDE.md:

- The LLM-touching EntityExtractionStep here uses a placeholder LLM
  stub. The real Ollama round-trip lives in the rag_e2e_synthesis
  surface tests (operator-run, auto-skip when daemon unreachable).

Why this file is in tests/integration not tests/unit
----------------------------------------------------
The whole tests/ tree is flat-integration in this repo per the
pyproject pytest config (no separate unit/integration split). Marker
``pytest.mark.integration`` keeps these in the same selection as
its sibling step-surface tests.

Stub LLM contract
-----------------
The ``_PlaceholderLLM`` class implements only ``.invoke(messages) ->
obj.content`` — exactly the surface
``apecx_db_integration.agent.extract_entities_llm`` consumes. FIFO
list of canned responses means one stub serves a function that calls
the LLM N times in sequence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

# Migrated 2026-04-27: entity-extraction agent now lives in
# ``apecx_integration.agents.violin_bvbrc``. The test patches
# ``_build_chat_llm`` at this NEW location so the wrapper step's real
# LLM call gets intercepted by ``_PlaceholderLLM``.
from apecx_integration.agents.violin_bvbrc import agent as _db_agent
from apecx_integration.composition.steps.db_integration_wrappers import (
    EntityExtractionStep,
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
    canned = json.dumps(
        [
            {"name": "EEEV", "type": "pathogen", "confidence": 0.95},
            {"name": "alphavirus vaccine", "type": "vaccine", "confidence": 0.7},
            {"name": "low-conf-noise", "type": "medical_term", "confidence": 0.2},
        ]
    )
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
