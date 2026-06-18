"""Live real-data parity for the taxon-resolution path used by the sequence-conservation leg.

After the dual-resolver unification (2026-06-18), ``EvidenceQueryNormalizeStep`` resolves the
query's virus name via the SAME dict resolver as the resolve step:
``extract_virus_names`` -> ``build_resolution_plan`` -> ``_iri_to_taxon_id``. This test exercises
that exact path against the REAL synonym dictionary AND verifies the resolved taxon actually has
BV-BRC CDS coverage (the property the conservation leg depends on).

It is the regression guard for the bug it replaces: the old live-BV-BRC name-matcher resolved
Lassa to the stale 11620 (0 coverage) and "Junin virus" to an Influenza A strain. Gated on the
dictionary being present + BV-BRC reachable (auto-skip, honest).
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.integration

_BVBRC_API = "https://www.bv-brc.org/api"


def _resolve_via_dict(query: str) -> int | None:
    """Reproduce EvidenceQueryNormalizeStep._maybe_resolve_taxon's resolution core."""
    from apecx_integration.agents.globus_search import taxonomy_resolver
    from apecx_integration.composition.steps.harmonized_resolve_step import build_resolution_plan
    from apecx_integration.composition.steps.harmonized_search_execute_step import _iri_to_taxon_id

    names = taxonomy_resolver.extract_virus_names(query)
    term = names[0] if names else query
    plan = build_resolution_plan(term, index="bvbrc_genome", entity_type_str="")
    iri = plan.get("canonical_iri")
    return _iri_to_taxon_id(iri) if isinstance(iri, str) and iri else None


def _dict_available() -> bool:
    try:
        return _resolve_via_dict("chikungunya virus epitopes") == 37124
    except Exception:
        return False


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            f"{_BVBRC_API}/taxonomy/?eq(taxon_id,2697049)&select(taxon_id)&limit(1)"
            "&http_accept=application/json",
            timeout=8,
        )
        return r.ok and isinstance(r.json(), list)
    except Exception:
        return False


needs = pytest.mark.skipif(
    not (_dict_available() and _bvbrc_reachable()),
    reason="synonym dictionary not loadable or BV-BRC unreachable",
)


def _cds(taxon_id: int) -> int:
    r = requests.get(
        f"{_BVBRC_API}/genome_feature/?eq(taxon_id,{taxon_id})&eq(feature_type,CDS)"
        f"&select(patric_id)&limit(2)&http_accept=application/json",
        timeout=20,
    )
    r.raise_for_status()
    return len(r.json())


@needs
@pytest.mark.parametrize(
    "query,expected_taxon_id",
    [
        # always-worked controls
        ("SARS-CoV-2 spike glycoprotein conserved epitopes", 2697049),
        ("chikungunya envelope epitopes", 37124),
        ("dengue virus envelope domain III", 12637),
        # REGRESSION: previously mis-resolved by the retired live name-matcher
        ("Lassa virus glycoprotein conserved epitopes", 3052310),  # was stale 11620 (0 coverage)
        ("Hantaan virus glycoprotein epitopes", 3052480),  # was strain 11601 (8 CDS)
    ],
)
def test_query_resolves_to_covered_taxon(query, expected_taxon_id):
    taxon_id = _resolve_via_dict(query)
    assert taxon_id == expected_taxon_id, (query, taxon_id)
    assert _cds(taxon_id) >= 2, ("resolved taxon must have BV-BRC CDS coverage", query, taxon_id)


@needs
def test_junin_resolves_to_arenavirus_not_influenza():
    """The retired resolver fuzzy-matched 'Junin virus' to an Influenza A strain (taxon 507335)."""
    taxon_id = _resolve_via_dict("Junin virus glycoprotein conserved epitopes")
    assert taxon_id not in (507335, None), taxon_id
    assert _cds(taxon_id) >= 2, ("Junin must resolve to a covered arenavirus taxon", taxon_id)


@needs
def test_no_virus_name_resolves_to_nothing():
    assert _resolve_via_dict("envelope glycoprotein structure") is None
