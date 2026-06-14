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


def test_unwraps_framework_envelope(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"normalize_input": {"query": "q", "x": 1}}))
    # The unwrapped fields pass through; a query with no virus name extracts no candidate,
    # so taxon resolution is attempted (no network — extraction is empty) and records a
    # NAMED no-taxon note rather than silently leaving the gap unexplained.
    assert out["query"] == "q"
    assert out["x"] == 1
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "no taxon resolved" in out["taxon_resolution"]["note"]


def test_resolves_taxon_when_caller_omits_it(tmp_path, monkeypatch):
    """No caller taxon_id -> resolve the virus name (BV-BRC wire mocked) and inject the
    taxon_id + canonical species name + provenance so the FULL-science legs unlock."""
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    tr._clear_cache()
    monkeypatch.setattr(
        tr,
        "_query_taxonomy",
        lambda name, *, api_base, timeout: [
            {
                "taxon_id": "2697049",
                "taxon_name": "Severe acute respiratory syndrome coronavirus 2",
                "genomes": 9407630,
            }
        ],
    )
    out = asyncio.run(
        _stage(tmp_path).process({"query": "SARS-CoV-2 spike glycoprotein conserved epitopes"})
    )
    assert out["taxon_id"] == 2697049
    assert out["resolved_species_name"] == "Severe acute respiratory syndrome coronavirus 2"
    prov = out["taxon_resolution"]
    assert prov["source"] == "bv-brc-taxonomy"
    assert prov["taxon_id"] == 2697049
    assert prov["genomes"] == 9407630
    tr._clear_cache()


def test_caller_taxon_id_is_left_untouched(tmp_path, monkeypatch):
    """A hand-supplied taxon_id must not be overridden, and the wire must not be hit."""
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    monkeypatch.setattr(tr, "_query_taxonomy", lambda *a, **k: pytest.fail("wire must not be hit"))
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv E1", "taxon_id": 37124}))
    assert out["taxon_id"] == 37124
    assert "taxon_resolution" not in out


def test_unresolvable_virus_name_records_named_note(tmp_path, monkeypatch):
    """A virus name that BV-BRC cannot resolve -> taxon_id stays absent + a NAMED note."""
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    tr._clear_cache()
    monkeypatch.setattr(tr, "_query_taxonomy", lambda name, *, api_base, timeout: [])
    out = asyncio.run(_stage(tmp_path).process({"query": "Unobtainium virus glycoprotein"}))
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "Unobtainium virus" in out["taxon_resolution"]["candidates"]
    assert "no taxon resolved" in out["taxon_resolution"]["note"]
    tr._clear_cache()


def test_missing_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"requested_outputs": "evidence_only"}))


def test_blank_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"query": "   "}))
