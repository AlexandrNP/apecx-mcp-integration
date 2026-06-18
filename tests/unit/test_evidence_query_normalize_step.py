"""Unit tests for EvidenceQueryNormalizeStep — the deposit-point passthrough."""

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


# The step resolves via the dict resolver (harmonized_resolve_step.build_resolution_plan). Patch
# THAT module attribute (the step imports it lazily at call time) for deterministic unit tests;
# the real-dict + real-BV-BRC parity lives in tests/integration/test_taxonomy_resolver_live.py.
def _patch_plan(monkeypatch, *, taxon_id=None, label=None, status="id_anchored"):
    import apecx_integration.composition.steps.harmonized_resolve_step as hrs

    iri = f"http://purl.obolibrary.org/obo/NCBITaxon_{taxon_id}" if taxon_id else None

    def fake(term, *, index, entity_type_str):
        return {"canonical_iri": iri, "canonical_label": label, "resolution_status": status}

    monkeypatch.setattr(hrs, "build_resolution_plan", fake)


def _patch_plan_must_not_run(monkeypatch):
    import apecx_integration.composition.steps.harmonized_resolve_step as hrs

    monkeypatch.setattr(
        hrs, "build_resolution_plan", lambda *a, **k: pytest.fail("resolver must not be hit")
    )


def test_passthrough_preserves_all_fields(tmp_path):
    """The full param dict must pass through unchanged so ONE output feeds both
    assemble (query) and the gate (control fields)."""
    params = {
        "query": "chikv E1",
        "taxon_id": 37124,
        "requested_outputs": "evidence_plus_design",
        "design_approval_id": "appr-1",
    }
    out = asyncio.run(_stage(tmp_path).process(dict(params)))
    assert out == params


def test_unwraps_framework_envelope(tmp_path, monkeypatch):
    # A query with no virus name resolves to nothing -> a NAMED no-taxon note (gap is explained,
    # not silent). The dict resolver is mocked to a miss for determinism.
    _patch_plan(monkeypatch, taxon_id=None, status="unresolved")
    out = asyncio.run(_stage(tmp_path).process({"normalize_input": {"query": "q", "x": 1}}))
    assert out["query"] == "q"
    assert out["x"] == 1
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "no taxon resolved" in out["taxon_resolution"]["note"]


def test_resolves_taxon_when_caller_omits_it(tmp_path, monkeypatch):
    """No caller taxon_id -> resolve the virus name via the dict resolver and inject the
    taxon_id + canonical species name + provenance so the FULL-science legs unlock."""
    _patch_plan(
        monkeypatch,
        taxon_id=2697049,
        label="Severe acute respiratory syndrome coronavirus 2",
    )
    out = asyncio.run(
        _stage(tmp_path).process({"query": "SARS-CoV-2 spike glycoprotein conserved epitopes"})
    )
    assert out["taxon_id"] == 2697049
    assert out["resolved_species_name"] == "Severe acute respiratory syndrome coronavirus 2"
    prov = out["taxon_resolution"]
    assert prov["source"] == "synonym-dictionary"
    assert prov["taxon_id"] == 2697049
    assert prov["canonical_iri"].endswith("NCBITaxon_2697049")


def test_caller_taxon_id_is_left_untouched(tmp_path, monkeypatch):
    """A hand-supplied taxon_id must not be overridden, and the resolver must not be hit."""
    _patch_plan_must_not_run(monkeypatch)
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv E1", "taxon_id": 37124}))
    assert out["taxon_id"] == 37124
    assert "taxon_resolution" not in out


def test_unresolvable_virus_name_records_named_note(tmp_path, monkeypatch):
    """A virus name the dictionary cannot map -> taxon_id stays absent + a NAMED note."""
    _patch_plan(monkeypatch, taxon_id=None, status="unresolved")
    out = asyncio.run(_stage(tmp_path).process({"query": "Unobtainium virus glycoprotein"}))
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "Unobtainium virus" in out["taxon_resolution"]["candidates"]
    assert "no taxon resolved" in out["taxon_resolution"]["note"]


def test_missing_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"requested_outputs": "evidence_only"}))


def test_blank_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"query": "   "}))
