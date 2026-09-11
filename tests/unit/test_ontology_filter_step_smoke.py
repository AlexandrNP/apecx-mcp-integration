"""Direct-step-process unit tests for OntologyFilterStep (no network, no dictionary).

The step is the TRUE NCBITaxon-IRI filter: it keeps only records whose stamped
``iris`` contain the envelope's ``target_iri``. Filtering is pure over the input —
organism->IRI RESOLUTION is a separate upstream concern — so these tests need no
network and no dictionary; they construct the step via ``from_config`` and call
``process()`` directly on a hand-built envelope.

Covered:
  * happy path: two stamped records, one matching target_iri -> only that one kept.
  * degrade-loud: missing target_iri -> status=no_target_iri, filtered_records==[].
  * runtime shape: the DU-wrapped ``{"filter_input": <envelope>}`` form filters too.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.composition.workflows.literature_rag.steps.ontology_filter_step import (
    OntologyFilterStep,
)

STEP_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "literature_rag"
    / "steps"
    / "ontology_filter_step.yml"
)

_MEASLES_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_DENGUE_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_12637"

_MEASLES_RECORD = {"pmid": "1", "iris": [_MEASLES_IRI]}
_DENGUE_RECORD = {"pmid": "2", "iris": [_DENGUE_IRI]}


def _step() -> OntologyFilterStep:
    return OntologyFilterStep.from_config(str(STEP_YAML))


async def test_filters_to_matching_iri_only():
    """Only the record stamped with target_iri survives; the other is dropped."""
    step = _step()
    out = await step.process(
        {
            "stamped_records": [_MEASLES_RECORD, _DENGUE_RECORD],
            "target_iri": _MEASLES_IRI,
        }
    )
    assert out["status"] == "ok"
    assert out["target_iri"] == _MEASLES_IRI
    assert out["filtered_records"] == [_MEASLES_RECORD]


async def test_missing_target_iri_degrades_loud():
    """No target_iri -> status=no_target_iri and an empty filtered set (no raise)."""
    step = _step()
    out = await step.process({"stamped_records": [_MEASLES_RECORD, _DENGUE_RECORD]})
    assert out["status"] == "no_target_iri"
    assert out["target_iri"] is None
    assert out["filtered_records"] == []


async def test_du_wrapped_runtime_envelope_filters():
    """The cascade delivers {"filter_input": <envelope>}; the step unwraps + filters it."""
    step = _step()
    out = await step.process(
        {
            "filter_input": {
                "stamped_records": [_MEASLES_RECORD, _DENGUE_RECORD],
                "target_iri": _DENGUE_IRI,
            }
        }
    )
    assert out["status"] == "ok"
    assert out["filtered_records"] == [_DENGUE_RECORD]
