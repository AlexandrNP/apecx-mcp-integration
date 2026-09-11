"""Smoke tests for the literature gazetteer skeleton.

Verifies the deterministic longest-match tagger and its precision guard on
injected in-memory data only — no network, no dictionary load.
"""

from __future__ import annotations

from apecx_integration.agents.literature.gazetteer import Tag, build_gazetteer

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_MAP = {"chikv": _CHIKV_IRI, "chikungunya virus": _CHIKV_IRI}


def test_tags_chikv_surface_to_taxon_iri():
    gaz = build_gazetteer(_MAP)

    tags = gaz.tag("CHIKV neutralizing antibodies")

    assert any(isinstance(t, Tag) and t.iri.endswith("NCBITaxon_37124") for t in tags)


def test_tagging_is_deterministic():
    gaz = build_gazetteer(_MAP)
    text = "CHIKV and chikungunya virus co-circulate"

    assert gaz.tag(text) == gaz.tag(text)


def test_blocklisted_generic_word_is_not_tagged():
    # "virus" is injected into the map but the precision guard must skip it.
    gaz = build_gazetteer({**_MAP, "virus": _CHIKV_IRI})

    tags = gaz.tag("this virus is dangerous")

    assert all(t.surface.lower() != "virus" for t in tags)
