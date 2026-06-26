"""Unit tests for EpitopeResolveStep.

The step resolves a BARE virus name extracted from the query into a canonical
plan + the 9-index destination fan-out list. The unit tests stub
``build_resolution_plan`` and ``extract_virus_names`` so the surface stays fast
+ deterministic (no live dictionary / network). The harmonized integration
test exercises the real resolver against Globus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.agents.globus_search import taxonomy_resolver
from apecx_integration.composition.steps import (
    harmonized_resolve_step,
)
from apecx_integration.composition.steps.epitope_resolve_step import EpitopeResolveStep
from apecx_integration.composition.steps.harmonized_search_execute_step import (
    _INDEX_UUIDS,
)


def _stage(tmp_path: Path) -> EpitopeResolveStep:
    p = tmp_path / "epitope_resolve.yml"
    p.write_text("name: epitope_resolve_test\n")
    return EpitopeResolveStep.from_config(str(p))


def _fast_plan(term: str = "Chikungunya virus") -> dict:
    return {
        "term": term,
        "index": "bvbrc_genome",
        "resolution_path": "fast",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        "canonical_label": "Chikungunya virus",
        "canonical_ontology": "ncbitaxon",
        "confidence": 1.0,
        "resolution_status": "id_anchored",
        "synonyms": ["CHIKV", "Chikungunya virus"],
        "candidates": [],
        "needs_disambiguation": False,
        "evidence": "dict hit",
    }


def _resolved_plan(term: str, iri_suffix: str, label: str) -> dict:
    return {
        "term": term,
        "index": "bvbrc_genome",
        "resolution_path": "dict",
        "canonical_iri": f"http://purl.obolibrary.org/obo/NCBITaxon_{iri_suffix}",
        "canonical_label": label,
        "canonical_ontology": "ncbitaxon",
        "confidence": 1.0,
        "resolution_status": "id_anchored",
        "synonyms": [],
        "candidates": [],
        "needs_disambiguation": False,
        "evidence": "dict hit",
    }


def _unresolved_plan(term: str) -> dict:
    return {
        "term": term,
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
        "evidence": "no hit",
    }


def _ambiguous_plan(term: str = "RSV") -> dict:
    return {
        "term": term,
        "index": "bvbrc_genome",
        "resolution_path": "ambiguous",
        "canonical_iri": None,
        "canonical_label": None,
        "canonical_ontology": None,
        "confidence": 0.0,
        "resolution_status": "id_anchored",
        "synonyms": [],
        "candidates": [
            {"canonical_iri": "http://x/NCBITaxon_11250", "canonical_label": "Human RSV"},
            {"canonical_iri": "http://x/NCBITaxon_11246", "canonical_label": "Bovine RSV"},
        ],
        "needs_disambiguation": True,
        "evidence": "2-way ambiguity",
    }


@pytest.fixture
def _patches(monkeypatch):
    """Patch build_resolution_plan + extract_virus_names where the step reads them."""
    yield monkeypatch


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "epitope_resolve_test"


def test_bare_name_flattens_plan_and_fans_out(tmp_path, _patches):
    captured = {}

    def _fake_extract(query):
        return ["Chikungunya virus"]

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        captured["term"] = term
        captured["index"] = index
        return _fast_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "epitopes of chikungunya", "protein": "E2"}))

    # virus name extracted; index placeholder is bvbrc_genome
    assert captured["term"] == "Chikungunya virus"
    assert captured["index"] == "bvbrc_genome"

    # flattened plan fields present, EXCEPT index (the map sets per-index index)
    assert "index" not in out
    assert out["term"] == "Chikungunya virus"
    assert out["resolution_path"] == "fast"
    assert out["canonical_iri"].endswith("NCBITaxon_37124")
    assert out["canonical_label"] == "Chikungunya virus"
    assert out["confidence"] == 1.0
    assert out["needs_disambiguation"] is False
    assert out["candidates"] == []

    # full plan + fan-out list
    assert out["resolution_plan"]["canonical_iri"].endswith("NCBITaxon_37124")
    assert out["index_names"] == sorted(_INDEX_UUIDS)
    assert len(out["index_names"]) == 9

    # passthrough
    assert out["query"] == "epitopes of chikungunya"
    assert out["protein"] == "E2"
    assert "resolution_note" not in out


def test_no_extracted_name_resolves_via_query_decomposition(tmp_path, _patches):
    """No alias extracted → the raw query is the first decomposition candidate. With only the
    full query resolving (realistic dict: sub-prefixes miss), the step resolves it and fans out
    across all 9 indices; the protein recovery finds no dropped suffix to recover."""

    def _fake_extract(query):
        return []

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        if term == "some obscure pathogen xyz":
            return _resolved_plan(term, "99999", "Some obscure pathogen")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "some obscure pathogen xyz"}))
    assert out["term"] == "some obscure pathogen xyz"
    assert out["canonical_iri"].endswith("NCBITaxon_99999")
    assert out["index_names"] == sorted(_INDEX_UUIDS)
    assert "protein" not in out  # full query resolved → no trailing suffix to recover


def test_combined_virus_protein_query_decomposes_and_recovers_protein(tmp_path, _patches):
    """A combined ``'<virus> <protein>'`` query for a virus NOT in the alias table must
    resolve to the canonical ``'<virus> virus'`` taxon via deterministic decomposition
    (drop the trailing token, append ``' virus'``, EXACT dict resolve — no LLM, no fuzzy
    match) AND recover ``protein`` so the conservation leg can run. This reproduces the
    real Claude-Desktop bug: ``run_workflow('viral_epitope_analysis', {'query': 'Mayaro E1'})``
    used to resolve the whole string ``'Mayaro E1'`` as one entity → unresolved → dead workflow.
    """
    seen: list[str] = []

    def _fake_extract(query):
        return []  # "Mayaro" is not in the curated alias table (the bug's precondition)

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        seen.append(term)
        # Mimic the REAL dict: only the canonical "<virus> virus" form resolves.
        if term == "Mayaro virus":
            return _resolved_plan(term, "59301", "Mayaro virus")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    out = asyncio.run(_stage(tmp_path).process({"query": "Mayaro E1"}))

    assert out["canonical_iri"].endswith("NCBITaxon_59301")
    assert out["resolution_status"] == "id_anchored"
    assert out["term"] == "Mayaro virus"
    assert out["protein"] == "E1"  # recovered from the dropped suffix → feeds the sequence leg
    assert out["index_names"] == sorted(_INDEX_UUIDS)  # the 9-index harmonized search runs
    assert "Mayaro virus" in seen  # decomposition actually reached the canonical form
    # the full combined string was tried first and missed (decomposition, not luck)
    assert seen[0] == "Mayaro E1"


def test_decomposition_resolves_bare_arbitrary_name_without_clobbering_protein(tmp_path, _patches):
    """A bare arbitrary name ('Mayaro', alias-table miss) resolves deterministically to the
    canonical form, and a caller-supplied ``protein`` is preserved (no suffix was dropped)."""

    def _fake_extract(query):
        return []

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        if term == "Mayaro virus":
            return _resolved_plan(term, "59301", "Mayaro virus")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    out = asyncio.run(_stage(tmp_path).process({"query": "Mayaro", "protein": "GPC"}))

    assert out["canonical_iri"].endswith("NCBITaxon_59301")
    assert out["term"] == "Mayaro virus"
    assert out["protein"] == "GPC"  # caller-supplied protein NOT clobbered


def test_alias_hit_combined_query_recovers_protein_via_same_taxon(tmp_path, _patches):
    """When the virus resolves via an extracted ALIAS name ('dengue NS1' → 'Dengue virus'), the
    protein is recovered as the longest query-prefix that resolves to the SAME taxon. This closes
    the combined-query class for alias-table viruses too (not just the alias-miss 'Mayaro' case)."""

    def _fake_extract(query):
        return ["Dengue virus"]  # alias hit — protein is NOT in the canonical name

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        # Real dict: both the alias canonical AND 'dengue virus' resolve to DENV; 'dengue NS1' misses.
        if term in ("Dengue virus", "dengue virus"):
            return _resolved_plan(term, "12637", "Dengue virus")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    out = asyncio.run(_stage(tmp_path).process({"query": "dengue NS1"}))

    assert out["canonical_iri"].endswith("NCBITaxon_12637")
    assert out["term"] == "Dengue virus"  # resolved via the alias, not the decomposition
    assert out["protein"] == "NS1"  # recovered same-taxon


def test_protein_recovery_is_safe_for_multiword_virus_names(tmp_path, _patches):
    """'West Nile virus E' must recover protein 'E', NOT 'virus E' — longest-prefix-first picks
    the full 'West Nile virus' before the shorter 'West Nile', so only the true trailing token is
    dropped. This pins the exact heuristic trap that shortest-first ordering would fall into."""

    def _fake_extract(query):
        return ["West Nile virus"]

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        if term == "West Nile virus":
            return _resolved_plan(term, "11082", "West Nile virus")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    out = asyncio.run(_stage(tmp_path).process({"query": "West Nile virus E"}))

    assert out["canonical_iri"].endswith("NCBITaxon_11082")
    assert out["protein"] == "E"


def test_bare_virus_name_recovers_no_protein(tmp_path, _patches):
    """A BARE '<X> virus' query (the most common case) must NOT recover a protein. Even when a
    shorter prefix ('West Nile') resolves to the same taxon, longest-prefix-first picks the full
    name first → suffix None → no protein. Regression pin for the 'protein=virus' bug (the recovery
    loop used to skip the None-suffix full query and drop the trailing 'virus' token as a protein)."""

    def _fake_extract(query):
        return ["West Nile virus"]

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        # BOTH the full name AND the bare prefix resolve to the same taxon — the trap.
        if term in ("West Nile virus", "West Nile"):
            return _resolved_plan(term, "11082", "West Nile virus")
        return _unresolved_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    out = asyncio.run(_stage(tmp_path).process({"query": "West Nile virus"}))

    assert out["canonical_iri"].endswith("NCBITaxon_11082")
    assert "protein" not in out  # nothing dropped → no protein (NOT 'virus')


def test_ambiguous_plan_noops_the_map(tmp_path, _patches):
    def _fake_extract(query):
        return ["RSV"]

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        return _ambiguous_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "RSV epitopes"}))

    assert out["needs_disambiguation"] is True
    assert out["index_names"] == []  # map no-ops
    assert len(out["_ambiguous_candidates"]) == 2
    assert "resolution_note" in out
    assert "AMBIGUOUS" in out["resolution_note"]


def test_unwraps_trigger_envelope(tmp_path, _patches):
    def _fake_extract(query):
        return ["Chikungunya virus"]

    def _fake_plan(term, index="bvbrc_genome", entity_type_str=""):
        return _fast_plan(term)

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _fake_plan)

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"resolve_input": {"query": "chikungunya epitopes"}}))
    assert out["term"] == "Chikungunya virus"
    assert out["query"] == "chikungunya epitopes"


def test_resolution_failure_degrades_loud(tmp_path, _patches):
    """A build_resolution_plan that raises must NOT crash the step — it degrades
    loud (resolution_note) and KEEPS the full fan-out for the raw fallback."""

    def _fake_extract(query):
        return ["Chikungunya virus"]

    def _boom(term, index="bvbrc_genome", entity_type_str=""):
        raise RuntimeError("simulated: synonym dictionary unavailable")

    _patches.setattr(taxonomy_resolver, "extract_virus_names", _fake_extract)
    _patches.setattr(harmonized_resolve_step, "build_resolution_plan", _boom)

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "chikungunya epitopes"}))
    assert out["resolution_path"] == "miss"
    assert out["canonical_iri"] is None
    assert out["index_names"] == sorted(_INDEX_UUIDS)  # fan-out kept for raw fallback
    assert "could not run" in out["resolution_note"]


@pytest.mark.parametrize(
    "bad_input",
    [
        {},
        {"query": ""},
        {"query": 123},
        {"protein": "E2"},
    ],
)
def test_missing_query_raises(tmp_path, bad_input):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="query"):
        asyncio.run(step.process(bad_input))


def test_non_dict_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process(["not", "a", "dict"]))


def test_caller_supplied_taxon_skips_resolution_but_keeps_index_names(tmp_path, monkeypatch):
    """REGRESSION: a pre-set NCBITaxon canonical_iri (caller-supplied taxon_id seeded by normalize)
    SKIPS the dict name-resolution WITHOUT emptying index_names — the harmonized map's item list.
    An early return here silently no-op'd the 9-index search. build_resolution_plan must NOT run;
    index_names + resolution_plan must still be set and canonical_iri preserved."""
    monkeypatch.setattr(
        harmonized_resolve_step,
        "build_resolution_plan",
        lambda *a, **k: pytest.fail(
            "build_resolution_plan must not run for a caller-supplied taxon"
        ),
    )
    bundle = {
        "query": "chikv E1",
        "taxon_id": 37124,
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
    }
    out = asyncio.run(_stage(tmp_path).process({"resolve_input": bundle}))
    assert out["canonical_iri"] == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert out["resolution_status"] == "caller_supplied"
    assert len(out["index_names"]) == len(_INDEX_UUIDS)  # 9-index fan-out preserved (the bug)
    assert "resolution_plan" in out
