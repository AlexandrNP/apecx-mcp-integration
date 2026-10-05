"""Unit tests for taxon-IRI grounding of the Globus + PubMed branches of
``SynthesisContextAssemblyStep`` (keep-untagged policy).

These mock the Globus ``search`` and inject a gazetteer (no network, no
dictionary SQLite). The mocks-parity partner that exercises the SAME grounding
against a real Globus index + real dictionary is
``tests/integration/test_rag_e2e_grounding_live.py`` (per the workspace
unit-mock / integration-test parity rule).
"""

from __future__ import annotations

from apecx_integration.composition.steps.synthesis_context_assembly_step import (
    SynthesisContextAssemblyStep,
)

_CHIKV = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_EEEV = "http://purl.obolibrary.org/obo/NCBITaxon_11021"


def _bare_step(*, ground: bool) -> SynthesisContextAssemblyStep:
    """A from_config-bypassing bare step (same shortcut as the branch-failure
    tests), with only the attributes the grounding helpers read."""
    step = object.__new__(SynthesisContextAssemblyStep)
    step.name = "test_grounding"
    step._ground_iri = ground
    step._max_globus = 10
    return step


# --- _globus_search: result-side IRI filter (keep-untagged) ---------------


def test_synthesis_assembly_grounds_globus_leg(monkeypatch):
    """A Globus hit whose taxon tag CONFLICTS is dropped; an untagged hit and
    an on-target hit are kept."""
    step = _bare_step(ground=True)
    hits = [
        {"subject": "on_target", "content": {"m": "chikv"}},
        {"subject": "conflict", "content": {"m": "eeev"}},
        {"subject": "untagged", "content": {"m": "none"}},
    ]
    taxon_by_marker = {"chikv": [_CHIKV], "eeev": [_EEEV], "none": []}

    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.search",
        lambda query, max_results: hits,
    )
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search._datacite.datacite_taxon_iris",
        lambda content: taxon_by_marker[content["m"]],
    )

    kept = step._globus_search("chikungunya virus", [_CHIKV])

    assert [h["subject"] for h in kept] == ["on_target", "untagged"]


def test_synthesis_assembly_globus_no_target_passthrough(monkeypatch):
    """Empty target_iris ⇒ every hit kept (no filtering)."""
    step = _bare_step(ground=True)
    hits = [{"subject": "a", "content": {}}, {"subject": "b", "content": {}}]
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.search",
        lambda query, max_results: hits,
    )
    assert step._globus_search("anything", []) == hits


# --- _ground_publications: stamp-then-filter (keep-untagged) --------------


def test_synthesis_assembly_grounds_pubmed_leg(monkeypatch):
    """Off-organism publication dropped; untagged + on-target kept. Uses a REAL
    gazetteer + real stamp_abstract over an injected surface→IRI map."""
    from apecx_integration.agents.literature.gazetteer import build_gazetteer

    gz = build_gazetteer({"chikungunya virus": _CHIKV, "eastern equine encephalitis virus": _EEEV})
    monkeypatch.setattr(
        "apecx_integration.agents.literature.gazetteer.build_from_dictionary",
        lambda db_path, **kw: gz,
    )
    monkeypatch.setattr(
        "apecx_integration.synonym_dictionary.loader.default_dictionary_path",
        lambda: "/unused/by/injected/gazetteer",
    )

    step = _bare_step(ground=True)
    pubs = [
        {"pmid": "1", "title": "Chikungunya virus E1", "abstract": "epitope mapping"},
        {"pmid": "2", "title": "Eastern equine encephalitis virus", "abstract": "neurotropism"},
        {"pmid": "3", "title": "Generic review", "abstract": "no organism named here"},
    ]

    kept = step._ground_publications(pubs, [_CHIKV])

    assert [p["pmid"] for p in kept] == ["1", "3"]
    assert _CHIKV in kept[0]["iris"]  # on-target pub carries the IRI
    assert kept[1]["iris"] == []  # untagged pub kept


def test_synthesis_assembly_pubmed_grounding_degrades_loud(monkeypatch, caplog):
    """If the gazetteer build fails, pubs pass UNFILTERED (degrade-loud, never crash)."""

    def _boom(db_path, **kw):
        raise RuntimeError("simulated: dictionary missing")

    monkeypatch.setattr(
        "apecx_integration.agents.literature.gazetteer.build_from_dictionary", _boom
    )
    monkeypatch.setattr(
        "apecx_integration.synonym_dictionary.loader.default_dictionary_path",
        lambda: "/unused",
    )
    step = _bare_step(ground=True)
    pubs = [{"pmid": "1", "title": "x", "abstract": "y"}]
    import logging

    with caplog.at_level(logging.WARNING):
        out = step._ground_publications(pubs, [_CHIKV])
    assert out == pubs  # unfiltered
    assert any("IRI-grounding unavailable" in r.message for r in caplog.records)


# --- _resolve_query_iris: passthrough + disabled --------------------------


def test_synthesis_assembly_no_organism_passthrough(monkeypatch):
    """Query with no resolvable organism ⇒ zero IRIs ⇒ branches pass through."""
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.taxonomy_resolver.extract_virus_names",
        lambda query: [],
    )
    step = _bare_step(ground=True)
    assert step._resolve_query_iris("a question with no virus name") == []


def test_synthesis_assembly_grounding_disabled():
    """ground_to_organism_iri=False ⇒ no resolution at all (no imports/DB touched)."""
    step = _bare_step(ground=False)
    assert step._resolve_query_iris("chikungunya virus E1 epitopes") == []


def test_synthesis_assembly_resolve_degrades_loud_on_dict_failure(monkeypatch, caplog):
    """A missing/locked dictionary (resolver raises) ⇒ _resolve_query_iris returns []
    (pass-through) + a loud warning, NOT a propagating exception that would silently
    empty the whole synthesis under Workflow.run (G127)."""
    import logging

    def _boom(query):
        raise OSError("simulated: synonym dictionary locked")

    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.taxonomy_resolver.extract_virus_names", _boom
    )
    step = _bare_step(ground=True)
    with caplog.at_level(logging.WARNING):
        assert step._resolve_query_iris("chikungunya virus E1 epitopes") == []
    assert any("resolution unavailable" in r.message for r in caplog.records)


def test_synthesis_assembly_resolve_unions_multiple_organisms(monkeypatch):
    """Multiple organisms in one query ⇒ union of IRIs (comparative queries)."""
    monkeypatch.setattr(
        "apecx_integration.agents.globus_search.taxonomy_resolver.extract_virus_names",
        lambda query: ["chikungunya virus", "eastern equine encephalitis virus"],
    )
    monkeypatch.setattr(
        "apecx_integration.agents.literature.resolve.resolve_organism_to_iri",
        lambda name: {"chikungunya virus": _CHIKV, "eastern equine encephalitis virus": _EEEV}.get(
            name
        ),
    )
    step = _bare_step(ground=True)
    assert sorted(step._resolve_query_iris("CHIKV vs EEEV")) == sorted([_CHIKV, _EEEV])
