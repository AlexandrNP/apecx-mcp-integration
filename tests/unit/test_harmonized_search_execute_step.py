"""Unit tests for HarmonizedSearchExecuteStep.

The step has three branches:
- ambiguous resolution → paused envelope, NO Globus queries run
- miss → miss envelope
- resolved → both Globus queries run via globus_sdk

The unit tests cover the first two branches deterministically. The
third branch is exercised by the live-Globus integration test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.harmonized_search_execute_step import (
    HarmonizedSearchExecuteStep,
)


def _stage(tmp_path: Path) -> HarmonizedSearchExecuteStep:
    p = tmp_path / "harmonized_search_execute.yml"
    p.write_text("name: harmonized_search_execute_test\n")
    return HarmonizedSearchExecuteStep.from_config(str(p))


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "harmonized_search_execute_test"


def test_ambiguous_emits_paused_envelope_without_globus(tmp_path):
    """Ambiguous resolution path: NO Globus queries, only the candidate
    list goes in the envelope."""
    step = _stage(tmp_path)
    plan = {
        "term": "RSV",
        "index": "bvbrc_genome",
        "resolution_path": "ambiguous",
        "canonical_iri": None,
        "canonical_label": None,
        "canonical_ontology": None,
        "confidence": 0.0,
        "resolution_status": "ambiguous",
        "synonyms": [],
        "candidates": [
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "Cand A",
                "canonical_ontology": "ncbitaxon",
                "confidence": 1.0,
            },
            {
                "canonical_iri": "http://x/B",
                "canonical_label": "Cand B",
                "canonical_ontology": "ncbitaxon",
                "confidence": 1.0,
            },
        ],
        "needs_disambiguation": True,
        "evidence": "ambiguous resolution",
    }
    out = asyncio.run(step.process(plan))
    env_in = out["envelope_input"]
    assert "data" in env_in
    bundle = env_in["data"]
    assert bundle["kind"] == "bundle"
    parts = bundle["parts"]
    assert parts["status"] == "paused_awaiting_disambiguation"
    assert parts["resolution"]["candidate_count"] == 2
    assert "next_action" in parts
    assert parts["next_action"]["kind"] == "re-invoke_with_chosen_iri"
    # Critical: no raw_query / harmonized_query keys — Globus was NOT run.
    assert "raw_query" not in parts
    assert "harmonized_query" not in parts
    assert "divergence" not in parts
    # Markdown surfaces all candidates for human display.
    md = env_in["markdown"]
    assert "RSV" in md
    assert "http://x/A" in md
    assert "http://x/B" in md


def test_miss_emits_miss_envelope(tmp_path):
    step = _stage(tmp_path)
    plan = {
        "term": "totally-made-up-term",
        "index": "bvbrc_genome",
        "resolution_path": "miss",
        "canonical_iri": None,
        "canonical_label": None,
        "canonical_ontology": None,
        "confidence": 0.0,
        "resolution_status": "unresolved",
        "synonyms": [],
        "candidates": [],
        "needs_disambiguation": False,
        "evidence": "no match",
    }
    out = asyncio.run(step.process(plan))
    env_in = out["envelope_input"]
    parts = env_in["data"]["parts"]
    assert parts["resolution"]["path"] == "miss"
    assert parts["status"] == "ok"
    assert "raw_query" not in parts


@pytest.mark.parametrize("missing_key", ["term", "index", "resolution_path"])
def test_missing_required_keys_raises(tmp_path, missing_key):
    step = _stage(tmp_path)
    plan = {"term": "X", "index": "bvbrc_genome", "resolution_path": "miss"}
    plan.pop(missing_key)
    with pytest.raises(ValueError, match=missing_key):
        asyncio.run(step.process(plan))


def test_unknown_index_loud(tmp_path):
    step = _stage(tmp_path)
    plan = {
        "term": "X",
        "index": "not_a_real_index",
        "resolution_path": "miss",
        "candidates": [],
        "synonyms": [],
    }
    with pytest.raises(ValueError, match="unknown index"):
        asyncio.run(step.process(plan))


def test_unwraps_trigger_envelope(tmp_path):
    """Framework wraps input as {du_name: payload}; the step must unwrap."""
    step = _stage(tmp_path)
    plan = {
        "term": "X",
        "index": "bvbrc_genome",
        "resolution_path": "miss",
        "candidates": [],
        "synonyms": [],
    }
    out = asyncio.run(step.process({"plan": plan}))
    assert "envelope_input" in out


def test_paused_bundle_validates_as_data_shape(tmp_path):
    """The Bundle the paused envelope emits MUST parse via parse_data_shape
    (EnvelopeStep will raise if it doesn't)."""
    from apecx_integration.composition.schemas.data_shapes import (
        parse_data_shape,
    )

    step = _stage(tmp_path)
    plan = {
        "term": "RSV",
        "index": "bvbrc_genome",
        "resolution_path": "ambiguous",
        "candidates": [
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "A",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
        ],
        "synonyms": [],
    }
    out = asyncio.run(step.process(plan))
    shape = parse_data_shape(out["envelope_input"]["data"])
    assert shape.kind == "bundle"


def test_quote_raw_term_helper():
    """Multi-token / non-alphanumeric raw terms get quoted for phrase match."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _quote_raw_term,
    )

    assert _quote_raw_term("CHIKV") == ("CHIKV", False)
    q, was = _quote_raw_term("Sindbis virus")
    assert q == '"Sindbis virus"' and was is True
    q, was = _quote_raw_term("HSV-2")
    assert q == '"HSV-2"' and was is True


def test_iri_to_taxon_id_helper():
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _iri_to_taxon_id,
    )

    assert _iri_to_taxon_id("http://purl.obolibrary.org/obo/NCBITaxon_37124") == 37124
    assert _iri_to_taxon_id("not-a-real-iri") is None
