"""Live (real Globus) tests for the taxon-precise structural query (E3-2).

Proves, against the real aggregate index e74bf12a, that:
  * the facet pre-pass enumerates the real multi-spelling CHIKV organism set,
  * the structured PDB query returns ONLY CHIKV-deposited structures (West Nile,
    which the free-text query leaks in, is excluded),
  * the EMDB required-token path stays taxon-locked,
  * an unresolvable taxon degrades loud (named note, never a silent dump).

Gated on Globus reachability (auto-skip, honest). CC-1 everywhere: every happy
path asserts NON-EMPTY real data; the degrade asserts a non-empty NAMED note.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_INDEX = "e74bf12a-d0dd-4d19-a965-03f4936db851"


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search(_INDEX, {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


needs_globus = pytest.mark.skipif(not _globus_reachable(), reason="Globus Search unreachable")


def _organisms(content) -> list[str]:
    from apecx_integration.agents.globus_search._datacite import datacite_organisms

    return datacite_organisms(content)


@needs_globus
def test_enumerate_organisms_returns_real_chikv_spellings():
    """E3-2.1 CC-1: the facet pre-pass enumerates >=3 real CHIKV spellings, canonical present."""
    from apecx_integration.agents.globus_search.structural_query import enumerate_organisms

    spellings = enumerate_organisms("chikungunya")
    assert len(spellings) >= 3, spellings
    assert "Chikungunya virus" in spellings  # canonical
    # Strain/case variants are real and must be captured (else match_any under-recalls).
    assert any("strain" in s.lower() for s in spellings), spellings
    # Every enumerated value is genuinely a CHIKV spelling (no co-deposited host leaked).
    assert all("chikungunya" in s.lower() for s in spellings), spellings


@needs_globus
def test_datacite_organisms_on_a_real_pdb_record():
    """E3-2.1 CC-1: a real PDB record yields >=1 organism via datacite_organisms."""
    from apecx_integration.agents.globus_search import client as globus_client

    hits = globus_client.search(
        "envelope glycoprotein",
        max_results=10,
        filters=[
            {"type": "match_any", "field_name": "publisher.name", "values": ["RCSB PDB"]},
            {
                "type": "match_any",
                "field_name": "pdb.polymer_entities.scientific_name",
                "values": ["Chikungunya virus"],
            },
        ],
    )
    assert hits, "expected >=1 CHIKV PDB record"
    orgs = _organisms(hits[0]["content"])
    assert len(orgs) >= 1
    assert any("chikungunya" in o.lower() for o in orgs)


@needs_globus
def test_pdb_structured_query_is_all_chikv_and_excludes_west_nile():
    """E3-2.3 CC-1 + before/after: the structured query is 100% CHIKV; West Nile gone.

    Pins the real improvement: the free-text query drags a West Nile virus structure
    (7E4K) into the top-10; the structured query does not, and every returned record
    is CHIKV-deposited.
    """
    from apecx_integration.agents.globus_search import client as globus_client
    from apecx_integration.agents.globus_search.structural_query import search_one_source

    # BEFORE — free-text, publisher-only: leaks other viruses' structures.
    before = globus_client.search(
        "chikungunya envelope",
        max_results=10,
        filters=[{"type": "match_any", "field_name": "publisher.name", "values": ["RCSB PDB"]}],
    )
    before_orgs = {o for h in before for o in _organisms(h["content"])}
    assert "West Nile virus" in before_orgs, (
        "expected the free-text baseline to leak West Nile — fixture stale?"
    )

    # AFTER — taxon-precise structured query.
    result = search_one_source("chikungunya envelope", "pdb", "RCSB PDB", taxon_id=37124)
    assert result.note is None, result.note
    assert len(result.hits) >= 1, "CC-1: structured CHIKV query must be non-empty"

    leaked = []
    every_record_is_chikv = True
    for h in result.hits:
        orgs = _organisms(h["content"])
        if not any("chikungunya" in o.lower() for o in orgs):
            every_record_is_chikv = False
        # No non-CHIKV VIRUS antigen may appear (host Fabs Homo sapiens/Mus musculus
        # are legitimate co-deposits and allowed).
        for o in orgs:
            ol = o.lower()
            if ol.endswith("virus") and "chikungunya" not in ol:
                leaked.append((h.get("subject"), o))

    assert every_record_is_chikv, "every structured hit must be CHIKV-deposited"
    assert "West Nile virus" not in {o for h in result.hits for o in _organisms(h["content"])}
    assert not leaked, f"non-CHIKV virus organisms leaked into structured result: {leaked}"


@needs_globus
def test_emdb_required_token_path_is_taxon_locked_or_named_no_hit():
    """E3-2.4 CC-1: EMDB hits all carry the taxon token in title/desc, or a NAMED no-hit."""
    from apecx_integration.agents.globus_search.structural_query import search_one_source

    result = search_one_source(
        "chikungunya envelope glycoprotein", "emdb", "Electron Microscopy Data Bank"
    )
    if not result.hits:
        # Genuine no-hit -> the absence must be NAMED (the caller renders it).
        assert result.note is not None and result.note.strip()
        return
    from apecx_integration.agents.globus_search._datacite import (
        datacite_description,
        datacite_title,
    )

    for h in result.hits:
        content = h.get("content") or {}
        text = f"{datacite_title(content) or ''} {datacite_description(content) or ''}".lower()
        assert "chikungunya" in text, (h.get("subject"), text[:160])


@needs_globus
def test_unresolvable_taxon_degrades_loud_not_silent():
    """E3-2.5 CC-1 degrade: a query with no resolvable species -> non-empty hits + named note."""
    from apecx_integration.agents.globus_search.structural_query import search_one_source

    # "envelope glycoprotein" carries no virus name and no taxon_id.
    result = search_one_source("envelope glycoprotein", "pdb", "RCSB PDB", taxon_id=None)
    assert result.note is not None and result.note.strip()
    assert "not taxon-locked" in result.note
    # Degrade still returns real data (CC-1): it does not silently empty out.
    assert len(result.hits) >= 1


@needs_globus
def test_facet_fallback_resolves_single_token_species_lassa():
    """Facet-validated token fallback: "Lassa mammarenavirus" (taxon 11620, NOT curated,
    single-token name _VIRUS_RE can't match) resolves via the PDB scientific_name facet.

    Regression for the dropped-hits bug: the most-specific token "lassa" (fewest facet
    buckets) is chosen over the genus "mammarenavirus" (over-matches every mammarenavirus),
    and because the facet match IS the taxon-lock, note MUST stay None so
    StructuralEvidenceStep keeps the hits.
    """
    from apecx_integration.agents.globus_search.structural_query import (
        resolve_species_terms,
        search_one_source,
    )

    res = resolve_species_terms("Lassa mammarenavirus", taxon_id=11620)
    assert res.note is None, res.note
    assert res.terms == ["lassa"], res.terms  # most-specific token, not the genus

    result = search_one_source(
        "Lassa mammarenavirus", "pdb", "RCSB PDB", taxon_id=11620, max_results=8
    )
    assert result.note is None, result.note
    assert len(result.hits) >= 1, "CC-1: facet-fallback query must return real structures"
    assert any("lassa" in o.lower() for o in result.organisms), result.organisms


@needs_globus
def test_facet_fallback_finds_nothing_for_garbage_query():
    """AC3: a query with no facetable token -> empty terms + a NAMED note (no false positive)."""
    from apecx_integration.agents.globus_search.structural_query import resolve_species_terms

    res = resolve_species_terms("asdfqwer zzzz", taxon_id=None)
    assert res.terms == []
    assert res.note is not None and "not taxon-locked" in res.note


@needs_globus
def test_curated_virus_name_path_unaffected_by_fallback():
    """No-regression: Mayaro virus (taxon 59301) still resolves via the "<X> virus" path
    with note=None and real hits — the fallback is only reached when that path is empty."""
    from apecx_integration.agents.globus_search.structural_query import search_one_source

    result = search_one_source("Mayaro virus", "pdb", "RCSB PDB", taxon_id=59301, max_results=8)
    assert result.note is None, result.note
    assert len(result.hits) >= 1
    assert any("mayaro" in o.lower() for o in result.organisms), result.organisms
