"""EntityExtractionStep passes `query` through so a direct entity_extraction ->
SynthesisContextAssemblyStep link is shape-compatible (the composer's natural wiring).

Root cause this fixes: EntityExtractionStep emitted {entities, query_terms} (no query), but its
wrapper documents feeding SynthesisContextAssemblyStep.assembly_input, which REQUIRES 'query' and
RAISES without it — so the composer produced a workflow that failed at runtime (it only passed the
spec-mode e2e because that test asserts RUN status, not output, per G127).

The LLM call (extract_entities_llm) is patched to a fixed return so this tests the passthrough
WRAPPER LOGIC, not the LLM; the real LLM path is covered by the composer-against-ollama e2e.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

import apecx_integration

_WRAPPER = (
    Path(apecx_integration.__file__).parent / "composition/_catalog_steps/entity_extraction.yml"
)


def test_process_passes_query_through(monkeypatch):
    from apecx_integration.composition.steps import db_integration_wrappers
    from apecx_integration.composition.steps.db_integration_wrappers import EntityExtractionStep

    monkeypatch.setattr(
        db_integration_wrappers.apecx_db_integration,
        "extract_entities_llm",
        lambda q: [{"name": "EEEV", "type": "pathogen", "confidence": 0.9}],
    )
    step = EntityExtractionStep.from_config(str(_WRAPPER))
    out = asyncio.run(step.process({"query": "find EEEV vaccines"}))
    assert out["query"] == "find EEEV vaccines"  # the passthrough (the fix)
    assert out["entities"] and out["query_terms"] == ["EEEV"]


def test_output_contract_compatible_with_assembly_input():
    # The fix's payoff: entity_extraction_output is now shape-compatible with
    # SynthesisContextAssemblyStep.assembly_input (both can declare/require `query`).
    from nanobrain.core.data_contract import compatible, parse_contract

    out_c = yaml.safe_load(_WRAPPER.read_text())["output_data_units"]["entity_extraction_output"][
        "contract"
    ]
    # assembly_input requires `query` (record). The producer now guarantees query+entities+query_terms.
    assembly_input_contract = {"kind": "record", "required": {"query": {"kind": "text"}}}
    ok, why = compatible(parse_contract(out_c), parse_contract(assembly_input_contract))
    assert ok, f"entity_extraction_output -> assembly_input must be compatible now; got {why!r}"
