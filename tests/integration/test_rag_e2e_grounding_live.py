"""Live mocks-parity partner for the SynthesisContextAssemblyStep grounding
unit tests (tests/unit/test_synthesis_assembly_grounding.py).

Exercises the REAL grounding path on both legs:
  - Globus leg: real aggregate index e74bf12a + real _datacite.datacite_taxon_iris,
    with the organism IRI resolved from the real synonym dictionary.
  - PubMed leg: real PubMed harvest + real gazetteer (built from the real
    dictionary) + real stamp_abstract.

Gated: skips when Globus is unreachable, the dictionary is absent, or (PubMed)
the network harvest yields nothing.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration

_CHIKV_ORGANISM = "Chikungunya virus"


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


def _resolved_chikv_iri() -> str | None:
    try:
        from apecx_integration.agents.literature.resolve import resolve_organism_to_iri
        from apecx_integration.synonym_dictionary.loader import default_dictionary_path

        if not default_dictionary_path().exists():
            return None
        return resolve_organism_to_iri(_CHIKV_ORGANISM)
    except Exception:
        return None


needs_globus = pytest.mark.skipif(not _globus_reachable(), reason="Globus Search unreachable")
_CHIKV_IRI = _resolved_chikv_iri()
needs_dict = pytest.mark.skipif(
    _CHIKV_IRI is None, reason="synonym dictionary absent or CHIKV unresolved"
)


def _bare_step():
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    step = object.__new__(SynthesisContextAssemblyStep)
    step.name = "grounding_live"
    step._ground_iri = True
    step._max_globus = 20
    step._max_publications = 20
    step._query_template = "{query}"
    return step


@needs_globus
@needs_dict
def test_rag_e2e_grounding_globus_leg_live():
    """Grounding the real Globus branch for a CHIKV query drops only records whose
    taxon tag is non-empty AND excludes the CHIKV IRI (off-organism); on-target and
    untagged records survive."""
    from apecx_integration.agents.globus_search._datacite import datacite_taxon_iris

    step = _bare_step()
    query = "chikungunya virus structural polyprotein"

    hits_all = step._globus_search(query, [])
    hits_grounded = step._globus_search(query, [_CHIKV_IRI])

    assert len(hits_grounded) <= len(hits_all)
    kept_subjects = {h["subject"] for h in hits_grounded}
    dropped = [h for h in hits_all if h["subject"] not in kept_subjects]

    # Every dropped record must be PROVABLY off-organism: a non-empty taxon tag
    # that does not include the CHIKV IRI. (Nothing untagged was dropped.)
    for h in dropped:
        rec_iris = datacite_taxon_iris(h.get("content") or {})
        assert rec_iris, f"dropped an UNTAGGED record (keep-untagged violated): {h['subject']}"
        assert _CHIKV_IRI not in rec_iris, f"dropped an ON-TARGET record: {h['subject']}"


@needs_dict
def test_rag_e2e_grounding_pubmed_leg_live():
    """Grounding the real PubMed branch: harvest real CHIKV papers, stamp with the
    real gazetteer, and confirm every kept paper is on-target or untagged (never a
    conflicting-taxon-only paper)."""
    from apecx_integration.composition.steps import _pubmed_helpers

    step = _bare_step()
    term = _pubmed_helpers.build_focused_term("chikungunya virus E1 epitopes", owner_name=step.name)
    try:
        pubs = asyncio.run(_pubmed_helpers.harvest(term, max_papers=20))
    except Exception as exc:  # network flake
        pytest.skip(f"PubMed harvest unavailable: {exc}")
    if not pubs:
        pytest.skip("PubMed returned no papers for the probe term")

    grounded = step._ground_publications(pubs, [_CHIKV_IRI])

    assert len(grounded) <= len(pubs)
    # Keep-untagged policy: every surviving paper is untagged OR carries the CHIKV IRI.
    for p in grounded:
        assert not p["iris"] or _CHIKV_IRI in p["iris"], p.get("pmid")
    # The grounding actually ran (iris field stamped onto every record it kept).
    assert all("iris" in p for p in grounded)
