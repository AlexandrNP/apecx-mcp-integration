"""Unit tests for StructuralEvidenceStep — the PDB/EMDB structural leg.

Focus: the no-silent-failure contract. A no-hit and a Globus outage must BOTH
produce a loud, named ``structural_note`` — never a silent empty list.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.agents.globus_search.client import GlobusSearchUnavailableError
from apecx_integration.composition.steps import structural_evidence_step as mod
from apecx_integration.composition.steps.structural_evidence_step import StructuralEvidenceStep


def _stage(tmp_path: Path, **cfg) -> StructuralEvidenceStep:
    p = tmp_path / "structural.yml"
    body = "name: structural_test\n"
    for k, v in cfg.items():
        body += f"{k}: {v}\n"
    p.write_text(body)
    return StructuralEvidenceStep.from_config(str(p))


def _bundle(**over):
    b = {"query": "chikungunya structural polyprotein", "globus_results": []}
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "structural_test"


def test_no_hit_is_loud_not_silent(tmp_path, monkeypatch):
    """Zero structural records -> a NAMED structural_note, not a silent empty list."""
    step = _stage(tmp_path)
    monkeypatch.setattr(mod, "_DEFAULT_PUBLISHERS", {"pdb": "RCSB PDB"})

    def _empty(*a, **k):
        return []

    # Facet pre-pass finds no organism spelling -> PDB degrades to free-text, which
    # also returns [] -> genuine no-hit.
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.facet", _empty)
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _empty)
    out = asyncio.run(step.process(_bundle()))
    assert out["structural_records"] == []
    assert out["structural_note"] is not None
    assert "No PDB or EMDB structural records" in out["structural_note"]
    assert "chikungunya structural polyprotein" in out["structural_note"]


def test_non_taxon_locked_hits_are_dropped_not_rendered(tmp_path, monkeypatch):
    """Regression (Mayaro nsP1 e2e, 2026-06-27): when no PDB organism matches, the free-text degrade
    can return an UNRELATED organism's structure (an influenza HA `3GBN` surfaced for a virus with no
    PDB entry). Those non-taxon-locked hits MUST be dropped from structural_records — never rendered /
    SASA-computed as this query's structural evidence — with the not-taxon-locked degrade named."""
    step = _stage(tmp_path)
    # PDB-only: PDB is the source that free-text-degrades to an unrelated organism (EMDB's q REQUIRES
    # the taxon token, so it can't surface a wrong organism). Set the instance attr — the step already
    # captured self._publishers from the default at init.
    step._publishers = {"pdb": "RCSB PDB"}
    # facet finds NO organism spelling -> PDB free-text degrade ...
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.facet", lambda *a, **k: [])
    # ... whose free-text search returns an UNRELATED structure (the bug shape).
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.client.search",
        lambda *a, **k: [
            {"subject": "pdb:3GBN", "content": {"title": "Influenza HA"}, "score": None}
        ],
    )
    out = asyncio.run(step.process(_bundle(query="conserved epitopes on Mayaro virus nsP1")))
    # the unrelated structure is NOT passed downstream for rendering, NOT merged for citation
    assert out["structural_records"] == [], "non-taxon-locked hits must be dropped, not rendered"
    assert all(h.get("subject") != "pdb:3GBN" for h in out["globus_results"])
    # ... but the degrade is named (loud), with the specific not-taxon-locked reason
    assert out["structural_note"] is not None
    assert "not taxon-locked" in out["structural_note"].lower()
    assert (
        "No PDB or EMDB structural records" not in out["structural_note"]
    )  # the specific note, not the generic


def test_species_name_from_dict_resolves_label(monkeypatch):
    """The taxon_id -> canonical species label helper: hit returns the label; a miss / bad input /
    outage returns None (never raises — a scope lookup must not break the structural leg)."""

    class _Entry:
        canonical_label = "Human immunodeficiency virus 1"

    class _Idx:
        def lookup_by_iri(self, iri):
            return _Entry() if iri.endswith("NCBITaxon_11676") else None

    monkeypatch.setattr(
        "apecx_integration.synonym_dictionary.loader.get_dictionary_index",
        lambda: (_Idx(), None),
    )
    assert mod._species_name_from_dict(11676) == "Human immunodeficiency virus 1"
    assert mod._species_name_from_dict("11676") == "Human immunodeficiency virus 1"
    assert mod._species_name_from_dict(99999999) is None  # dict miss
    assert mod._species_name_from_dict("not-an-int") is None  # bad input


def test_dict_routing_taxon_locks_arbitrary_virus(tmp_path, monkeypatch):
    """Regression (SARS/HIV/Ebola, 2026-06-27): an arbitrary virus passed by taxon_id with NO
    resolved_species_name and NOT in the curated map now taxon-locks — the step resolves taxon_id ->
    canonical name via the dictionary so the facet pre-pass finds the organism (these used to get
    ZERO structures and silently degrade)."""
    step = _stage(tmp_path)
    step._publishers = {"pdb": "RCSB PDB"}
    monkeypatch.setattr(
        mod, "_species_name_from_dict", lambda tid: "Human immunodeficiency virus 1"
    )
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.client.facet",
        lambda *a, **k: [("Human immunodeficiency virus 1", 9)],
    )
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.client.search",
        lambda *a, **k: [{"subject": "pdb:1CE0", "content": {"title": "HIV gp120"}, "score": None}],
    )
    out = asyncio.run(
        step.process({"query": "HIV-1 gp120 epitopes", "taxon_id": 11676, "globus_results": []})
    )
    assert out["structural_note"] is None  # taxon-locked, NOT degraded
    assert any(r.get("subject") == "pdb:1CE0" for r in out["structural_records"])


def test_globus_outage_is_loud_and_distinct_from_no_hit(tmp_path, monkeypatch):
    """A Globus failure sets a DIFFERENT loud note ('unavailable') — not a no-hit."""
    step = _stage(tmp_path)

    def _boom(*a, **k):
        raise GlobusSearchUnavailableError("network down")

    # Facet returns nothing -> PDB degrades to free-text, which raises -> the
    # outage propagates (distinct from a no-hit).
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.facet", lambda *a, **k: [])
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _boom)
    out = asyncio.run(step.process(_bundle()))
    assert out["structural_records"] == []
    assert out["structural_note"] is not None
    assert "unavailable" in out["structural_note"].lower()
    assert "No PDB or EMDB structural records" not in out["structural_note"]


def test_hits_merge_into_globus_results_deduped_and_tagged(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(mod, "_DEFAULT_PUBLISHERS", {"pdb": "RCSB PDB", "emdb": "EMDB"})

    def _fake_search(query, *, max_results, filters, **k):
        pub = filters[0]["values"][0]
        if pub == "RCSB PDB":
            return [{"subject": "pdb:1I9G", "content": {"title": "X"}, "score": None}]
        return [{"subject": "emdb:EMD-1", "content": {"title": "Y"}, "score": None}]

    # Facet pre-pass enumerates the CHIKV spelling so the PDB query is taxon-locked.
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.client.facet",
        lambda *a, **k: [("Chikungunya virus", 5)],
    )
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _fake_search)
    # Pre-seed globus_results with a duplicate pdb:1I9G to prove dedup-by-subject.
    out = asyncio.run(
        step.process(_bundle(globus_results=[{"subject": "pdb:1I9G", "content": {"title": "old"}}]))
    )
    assert out["structural_note"] is None
    subjects = [h["subject"] for h in out["globus_results"]]
    assert subjects.count("pdb:1I9G") == 1  # deduped
    assert "emdb:EMD-1" in subjects  # new one merged
    # records carry the source tag for deterministic rendering downstream
    tags = {r["subject"]: r.get("structural_source") for r in out["structural_records"]}
    assert tags["pdb:1I9G"] == "pdb" and tags["emdb:EMD-1"] == "emdb"


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    """process({structural_input: bundle}) behaves like process(bundle)."""
    step = _stage(tmp_path)
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.facet", lambda *a, **k: [])
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", lambda *a, **k: [])
    out = asyncio.run(step.process({"structural_input": _bundle()}))
    assert out["query"] == "chikungunya structural polyprotein"
    assert out["structural_note"] is not None


def test_missing_query_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"globus_results": []}))
