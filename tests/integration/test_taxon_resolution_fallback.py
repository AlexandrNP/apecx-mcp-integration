"""Real-data integration tests for the LLM-driven taxon-resolution fallback chain.

Mock/integration parity for the unit tests (which mock the LLM + BV-BRC wire):
- BvbrcTaxonomySearchStep is exercised against the REAL BV-BRC taxonomy API (no LLM needed).
- The full synonym_gen -> bvbrc_search -> taxon_review chain is exercised against a REAL LLM +
  real BV-BRC when both are available (auto-skip otherwise — honest).

The fallback only fires on a dict MISS (no NCBITaxon canonical_iri on the bundle); these tests
seed a bundle WITHOUT canonical_iri so the steps actually run (not short-circuit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.integration

_BVBRC_API = "https://www.bv-brc.org/api"


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            f"{_BVBRC_API}/taxonomy/?eq(taxon_id,11620)&select(taxon_id)&limit(1)"
            "&http_accept=application/json",
            timeout=8,
        )
        return r.ok and isinstance(r.json(), list)
    except Exception:
        return False


def _llm_available() -> bool:
    try:
        from langchain_core.messages import HumanMessage

        from apecx_integration.agents._llm_config import preflight_llm_model
        from apecx_integration.agents._llm_factory import build_chat_llm

        preflight_llm_model()
        build_chat_llm(temperature=0.0, max_tokens=8).invoke([HumanMessage(content="ok?")])
        return True
    except Exception:
        return False


needs_bvbrc = pytest.mark.skipif(not _bvbrc_reachable(), reason="BV-BRC taxonomy API unreachable")
needs_llm = pytest.mark.skipif(not _llm_available(), reason="no apecx LLM (Ollama) reachable")


def _step(tmp_path: Path, module: str, cls: str):
    import importlib

    p = tmp_path / f"{cls}.yml"
    p.write_text(f"name: {cls}_test\n")
    return getattr(importlib.import_module(module), cls).from_config(str(p))


_CHAIN = [
    (
        "apecx_integration.composition.steps.taxon_synonym_generation_step",
        "TaxonSynonymGenerationStep",
        "synonym_gen_input",
    ),
    (
        "apecx_integration.composition.steps.bvbrc_taxonomy_search_step",
        "BvbrcTaxonomySearchStep",
        "bvbrc_search_input",
    ),
    (
        "apecx_integration.composition.steps.taxon_candidate_review_step",
        "TaxonCandidateReviewStep",
        "taxon_review_input",
    ),
]


def _run_fallback_chain(tmp_path: Path, query: str) -> dict:
    """Drive synonym_gen -> bvbrc_search -> taxon_review on a fresh cache for one query."""
    from apecx_integration.composition.steps.taxon_candidate_review_step import _clear_cache

    _clear_cache()
    bundle: dict = {"query": query}
    for module, cls, key in _CHAIN:
        bundle = asyncio.run(_step(tmp_path, module, cls).process({key: bundle}))
    return bundle


@needs_bvbrc
def test_bvbrc_taxonomy_search_ranks_real_taxa_by_cds(tmp_path):
    """Deterministic step, real BV-BRC: synonyms -> candidates ranked by EXACT CDS coverage
    (the fetchable signal), tie-broken by genome count."""
    step = _step(
        tmp_path,
        "apecx_integration.composition.steps.bvbrc_taxonomy_search_step",
        "BvbrcTaxonomySearchStep",
    )
    bundle = {"query": "Lassa virus", "taxon_synonyms": ["Lassa mammarenavirus", "Lassa virus"]}
    out = asyncio.run(step.process({"bvbrc_search_input": bundle}))
    cands = out["taxon_candidates"]
    assert cands, "real BV-BRC should return at least one Lassa taxonomy candidate"
    assert all({"taxon_id", "taxon_name", "genomes", "cds"} <= set(c) for c in cands)
    # ranked by cds desc (tie-break genomes desc): the per-candidate key is non-increasing.
    keys = [(c["cds"], c["genomes"]) for c in cands]
    assert keys == sorted(keys, reverse=True)


@needs_bvbrc
def test_bvbrc_search_surfaces_covered_clade_for_rotavirus(tmp_path):
    """Coverage-maximizing rank, real BV-BRC: a genus-structured virus (rotavirus) surfaces a
    descendant clade/species with substantial exact CDS as the TOP candidate — not the thin genus.
    Numbers shift over time, so assert a threshold (top candidate has real fetchable coverage)."""
    step = _step(
        tmp_path,
        "apecx_integration.composition.steps.bvbrc_taxonomy_search_step",
        "BvbrcTaxonomySearchStep",
    )
    bundle = {"query": "rotavirus", "taxon_synonyms": ["Rotavirus", "Rotavirus A"]}
    out = asyncio.run(step.process({"bvbrc_search_input": bundle}))
    cands = out["taxon_candidates"]
    assert cands, "real BV-BRC should return rotavirus taxonomy candidates"
    # the top candidate is CDS-covered (well above min_cds) — the conservation leg can fetch it.
    assert cands[0]["cds"] >= 1000, f"top rotavirus candidate thinly covered: {cands[0]}"


@needs_bvbrc
def test_fallback_chain_short_circuits_when_already_resolved(tmp_path):
    """When the dict resolver already set canonical_iri, all three steps pass through untouched
    (no LLM, no BV-BRC hit needed) — the common dict-hit path."""
    seeded = {
        "query": "chikungunya virus",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
    }
    for module, cls, key in [
        (
            "apecx_integration.composition.steps.taxon_synonym_generation_step",
            "TaxonSynonymGenerationStep",
            "synonym_gen_input",
        ),
        (
            "apecx_integration.composition.steps.bvbrc_taxonomy_search_step",
            "BvbrcTaxonomySearchStep",
            "bvbrc_search_input",
        ),
        (
            "apecx_integration.composition.steps.taxon_candidate_review_step",
            "TaxonCandidateReviewStep",
            "taxon_review_input",
        ),
    ]:
        step = _step(tmp_path, module, cls)
        out = asyncio.run(step.process({key: dict(seeded)}))
        assert out["canonical_iri"].endswith("NCBITaxon_37124")
        assert "taxon_synonyms" not in out  # synonym_gen short-circuited
    # taxon_review finalizes the int taxon_id from the IRI for the sequence leg/gate
    review = _step(
        tmp_path,
        "apecx_integration.composition.steps.taxon_candidate_review_step",
        "TaxonCandidateReviewStep",
    )
    out = asyncio.run(review.process({"taxon_review_input": dict(seeded)}))
    assert out["taxon_id"] == 37124


@needs_bvbrc
@needs_llm
def test_full_fallback_chain_resolves_real_dict_miss(tmp_path):
    """Real LLM + real BV-BRC: a bundle with NO canonical_iri runs the whole chain and resolves to
    a covered taxon, OR a named miss — never a wrong silently-promoted taxon."""
    bundle = _run_fallback_chain(tmp_path, "Lassa virus glycoprotein conserved epitopes")
    # Lassa is well-covered in BV-BRC — the chain should resolve it to a real arenavirus taxon
    # with CDS coverage (NOT a wrong organism). If the local model is too weak it may miss, which
    # is acceptable (degrade-loud) — but it must NEVER promote a non-arenavirus.
    if bundle.get("taxon_id") is not None:
        assert bundle["resolution_status"] == "llm_fallback"
        assert bundle["taxon_resolution"]["cds"] >= 2
    else:
        assert bundle["taxon_resolution"]["taxon_id"] is None  # honest named miss


@needs_bvbrc
@needs_llm
@pytest.mark.parametrize("query", ["norovirus epitopes", "rotavirus conserved sites"])
def test_full_fallback_chain_picks_covered_clade_not_thin_genus(tmp_path, query):
    """Real LLM + real BV-BRC, GENUS-STRUCTURED viruses: this exercises the multi-match -> max-CDS
    selection branch. The candidate set contains BOTH a thin genus (~0 exact CDS) and a richer
    descendant clade; the LLM confirms several are the same virus, and the step MUST select the
    HIGHEST-CDS one — never settle on the genus the conservation leg can't fetch.

    The assertion is RELATIVE, not an absolute floor: the resolver must promote the highest-CDS
    candidate it saw (which is provably never the ~0-CDS genus), and that taxon need only clear
    production's min_cds. This deliberately avoids a magic threshold — an absolute floor like
    `cds >= 1000` would conflate "beat the thin genus" (the real intent) with "has lots of data"
    and would WRONGLY fail a genus-structured virus whose richest clade is legitimately sparse.
    A degrade-loud miss stays acceptable (a weak local model may match nothing)."""
    bundle = _run_fallback_chain(tmp_path, query)
    if bundle.get("taxon_id") is None:
        assert bundle["taxon_resolution"]["taxon_id"] is None  # honest named miss
        return
    assert bundle["resolution_status"] == "llm_fallback"
    cands = bundle["taxon_candidates"]
    assert cands, "a fallback resolution must have gone through BV-BRC candidates"
    # coverage-max contract: the promoted taxon IS the highest-CDS candidate (compare by identity,
    # robust to BV-BRC counts drifting a hair between the rank probe and the winner re-verify).
    top = max(cands, key=lambda c: c["cds"])
    assert bundle["taxon_id"] == top["taxon_id"], (
        f"{query!r}: resolver did not pick the richest covered clade (the coverage-max bug): "
        f"resolved={bundle['taxon_id']}, candidates={[(c['taxon_id'], c['cds']) for c in cands]}"
    )
    # production contract: a promoted taxon clears min_cds — covered, however sparsely.
    assert bundle["taxon_resolution"]["cds"] >= 2


def _dict_resolves(term: str) -> bool:
    """True when the synonym dictionary is built and resolves ``term`` to a canonical taxon."""
    try:
        from apecx_integration.composition.steps.harmonized_resolve_step import (
            build_resolution_plan,
        )

        plan = build_resolution_plan(term, index="bvbrc_genome", entity_type_str="")
        return bool(plan.get("canonical_iri"))
    except Exception:
        return False


needs_dict = pytest.mark.skipif(
    not _dict_resolves("Mayaro virus"),
    reason="synonym dictionary not built / 'Mayaro virus' not resolvable",
)


@needs_dict
def test_combined_virus_protein_query_resolves_deterministically_no_llm(tmp_path):
    """REAL-DICT parity for the unit decomposition tests. A combined ``'<virus> <protein>'`` query
    resolves to the canonical taxon AND recovers the protein using the EXACT dict path — NO LLM
    (desktop locus), NO BV-BRC fallback. This is the precise path that died in real Claude Desktop:
    ``run_workflow('viral_epitope_analysis', {'query': 'Mayaro E1'})`` resolved the whole string as
    one entity → unresolved → dead workflow. Closes that gap for an alias-table MISS (Mayaro/Junin)
    AND an alias-table HIT (dengue), the latter via same-taxon protein recovery."""
    step = _step(
        tmp_path, "apecx_integration.composition.steps.epitope_resolve_step", "EpitopeResolveStep"
    )

    # alias-table MISS + appended protein — the reported failure.
    mayaro = asyncio.run(step.process({"query": "Mayaro E1"}))
    assert mayaro["canonical_iri"].endswith("NCBITaxon_59301")  # Mayaro virus
    assert mayaro["resolution_status"] == "id_anchored"
    assert mayaro["protein"] == "E1"
    assert mayaro["index_names"], "the 9-index harmonized fan-out must run on a clean hit"

    # arenavirus whose canonical is '<x> mammarenavirus' — '<x> virus' still resolves via the dict.
    junin = asyncio.run(step.process({"query": "Junin GPC"}))
    assert junin["canonical_iri"].endswith("NCBITaxon_2169991")
    assert junin["protein"] == "GPC"

    # alias-table HIT + appended protein — same-taxon recovery keeps the protein for the conservation leg.
    dengue = asyncio.run(step.process({"query": "dengue NS1"}))
    assert dengue["canonical_iri"].endswith("NCBITaxon_12637")  # Dengue virus
    assert dengue["protein"] == "NS1"

    # BARE '<X> virus' queries (the most common case) must recover NO protein — the trailing
    # 'virus' token is part of the organism name, never a protein (the 'protein=virus' regression).
    for bare in ("West Nile virus", "dengue virus", "Mayaro virus"):
        out = asyncio.run(step.process({"query": bare}))
        assert out.get("canonical_iri"), f"{bare!r} should resolve"
        assert out.get("protein") is None, (
            f"{bare!r} must not recover a protein, got {out.get('protein')!r}"
        )
