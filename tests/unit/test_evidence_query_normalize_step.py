"""Unit tests for EvidenceQueryNormalizeStep — the parse-only deposit-point passthrough.

Taxon RESOLUTION no longer happens here: it lives once in the resolve->LLM-fallback chain
(EpitopeResolveStep + taxon_synonym_generation/bvbrc_taxonomy_search/taxon_candidate_review),
whose output feeds the sequence leg + gate. normalize only parses/passes through and seeds
canonical_iri from a caller-supplied taxon_id so that chain short-circuits and honors it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.evidence_query_normalize_step import (
    EvidenceQueryNormalizeStep,
)


def _stage(tmp_path: Path) -> EvidenceQueryNormalizeStep:
    p = tmp_path / "normalize.yml"
    p.write_text("name: normalize_test\n")
    return EvidenceQueryNormalizeStep.from_config(str(p))


def test_passthrough_preserves_control_fields(tmp_path):
    """The control fields pass through unchanged so ONE output feeds resolve + (downstream) gate."""
    params = {
        "query": "chikv E1",
        "requested_outputs": "evidence_plus_design",
        "design_approval_id": "appr-1",
    }
    out = asyncio.run(_stage(tmp_path).process(dict(params)))
    for k, v in params.items():
        assert out[k] == v
    # No virus-name resolution happens here, so no taxon is injected.
    assert "taxon_id" not in out
    assert "canonical_iri" not in out


def test_unwraps_framework_envelope(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"normalize_input": {"query": "q", "x": 1}}))
    assert out["query"] == "q"
    assert out["x"] == 1
    assert "taxon_id" not in out  # parse-only; resolution is downstream


def test_caller_supplied_taxon_id_seeds_canonical_iri(tmp_path):
    """A hand-supplied taxon_id is honored: normalize seeds canonical_iri so the resolve->fallback
    chain short-circuits on it (resolve skips when canonical_iri is already an NCBITaxon IRI)."""
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv E1", "taxon_id": 37124}))
    assert out["taxon_id"] == 37124
    assert out["canonical_iri"] == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    # also accepts a digit-string taxon_id
    out2 = asyncio.run(_stage(tmp_path).process({"query": "x", "taxon_id": "37124"}))
    assert out2["taxon_id"] == 37124
    assert out2["canonical_iri"].endswith("NCBITaxon_37124")


def test_missing_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"requested_outputs": "evidence_only"}))


def test_blank_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"query": "   "}))
