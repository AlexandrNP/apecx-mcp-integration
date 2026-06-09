"""Unit tests for HarmonizedResolveStep.

The step is a thin wrapper around
``apecx_integration.synonym_dictionary.lookup.lookup_entity``. The
unit tests stub the lookup function so the test surface stays fast +
deterministic; the integration test (test_harmonized_search_workflow.py)
exercises the real dictionary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path

import pytest

from apecx_integration.composition.steps import harmonized_resolve_step
from apecx_integration.composition.steps.harmonized_resolve_step import (
    HarmonizedResolveStep,
)
from apecx_integration.synonym_dictionary.enums import ResolutionStatus
from apecx_integration.synonym_dictionary.lookup import LookupResult


def _stage(tmp_path: Path) -> HarmonizedResolveStep:
    """Materialize a from_config-loaded step from a minimal YAML."""
    p = tmp_path / "harmonized_resolve.yml"
    p.write_text("name: harmonized_resolve_test\n")
    return HarmonizedResolveStep.from_config(str(p))


def _stub_lookup(*results: LookupResult, expected_term: str | None = None):
    """Return a stub callable that yields the queued LookupResults."""
    queue = list(results)

    def stub(term, *, entity_type=None):
        if expected_term is not None and term != expected_term:
            raise AssertionError(f"stub called with term={term!r}, expected {expected_term!r}")
        if not queue:
            raise AssertionError(
                f"stub exhausted: called with term={term!r} but no more results queued"
            )
        return queue.pop(0)

    return stub


@pytest.fixture
def _restore_lookup(monkeypatch):
    """Snapshot lookup_entity so each test can monkeypatch freely."""
    original = harmonized_resolve_step.lookup_entity
    yield monkeypatch
    monkeypatch.setattr(harmonized_resolve_step, "lookup_entity", original)


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "harmonized_resolve_test"


def test_fast_path(tmp_path, _restore_lookup):
    fake = LookupResult(
        surface_form="CHIKV",
        path="fast",
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        canonical_ontology="ncbitaxon",
        confidence=1.0,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        synonyms=("CHIKV", "Chikungunya virus", "chik"),
        evidence="dict hit",
    )
    _restore_lookup.setattr(
        harmonized_resolve_step,
        "lookup_entity",
        _stub_lookup(fake, expected_term="CHIKV"),
    )
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"term": "CHIKV", "index": "bvbrc_genome"}))
    plan = out["plan"]
    assert plan["resolution_path"] == "fast"
    assert plan["canonical_iri"].endswith("NCBITaxon_37124")
    assert plan["canonical_label"] == "Chikungunya virus"
    assert plan["needs_disambiguation"] is False
    assert plan["candidates"] == []
    assert plan["index"] == "bvbrc_genome"
    assert plan["synonyms"] == ["CHIKV", "Chikungunya virus", "chik"]


def test_ambiguous_path_via_lookup_any_type(tmp_path, _restore_lookup):
    """Ambiguity is detected by querying lookup_any_type for multiple distinct
    canonical IRIs. The resolver's primary result might still claim 'fast'
    (first match wins) but the step overrides to 'ambiguous' when a
    multi-IRI conflict exists.
    """
    from datetime import datetime

    from apecx_integration.synonym_dictionary.enums import OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    fake_lookup_entity = LookupResult(
        surface_form="RSV",
        path="fast",  # resolver picks one optimistically
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_11250",
        canonical_label="Human orthopneumovirus",
        canonical_ontology="ncbitaxon",
        confidence=1.0,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        synonyms=("RSV",),
        evidence="",
    )

    def _entry(iri: str, label: str) -> DictionaryEntry:
        return DictionaryEntry(
            entity_type="pathogen",
            canonical_iri=iri,
            canonical_label=label,
            synonyms=(),
            ontology=OntologyName.NCBITAXON,
            ontology_version="test",
            source_records=(),
            confidence=1.0,
            resolved_at=datetime.now(UTC),
        )

    class _FakeIndex:
        def lookup_ambiguous_surface_forms(self, *, surface_form, limit=50):
            # Mimic the production dict shape: rows of (winning,
            # alternative) IRI pairs for the same normalized surface.
            return [
                {
                    "entity_type": "pathogen",
                    "surface_form_normalized": "rsv",
                    "winning_canonical_iri": "http://x/NCBITaxon_11250",
                    "alternative_canonical_iri": "http://x/NCBITaxon_11246",
                },
            ]

        def lookup_any_type(self, term):
            return [
                _entry("http://x/NCBITaxon_11250", "Human RSV"),
                _entry("http://x/NCBITaxon_11246", "Bovine RSV"),
            ]

        def lookup_by_iri(self, iri):
            labels = {
                "http://x/NCBITaxon_11250": "Human RSV",
                "http://x/NCBITaxon_11246": "Bovine RSV",
            }
            label = labels.get(iri)
            if label is None:
                return None
            return _entry(iri, label)

    def _fake_get_dictionary_index():
        return (_FakeIndex(), None)

    _restore_lookup.setattr(
        harmonized_resolve_step,
        "lookup_entity",
        _stub_lookup(fake_lookup_entity, expected_term="RSV"),
    )
    _restore_lookup.setattr(
        harmonized_resolve_step,
        "get_dictionary_index",
        _fake_get_dictionary_index,
    )

    step = _stage(tmp_path)
    out = asyncio.run(step.process({"term": "RSV", "index": "bvbrc_genome"}))
    plan = out["plan"]
    assert plan["resolution_path"] == "ambiguous"
    assert plan["canonical_iri"] is None  # cleared on ambiguity override
    assert plan["needs_disambiguation"] is True
    assert len(plan["candidates"]) == 2
    iris = {c["canonical_iri"] for c in plan["candidates"]}
    assert iris == {"http://x/NCBITaxon_11250", "http://x/NCBITaxon_11246"}


def test_miss_path(tmp_path, _restore_lookup):
    fake = LookupResult(
        surface_form="totally-made-up",
        path="miss",
        canonical_iri=None,
        canonical_label=None,
        canonical_ontology=None,
        confidence=0.0,
        resolution_status=ResolutionStatus.UNRESOLVED,
        synonyms=(),
        evidence="no match in dictionary",
    )
    _restore_lookup.setattr(
        harmonized_resolve_step,
        "lookup_entity",
        _stub_lookup(fake),
    )
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"term": "totally-made-up", "index": "bvbrc_genome"}))
    plan = out["plan"]
    assert plan["resolution_path"] == "miss"
    assert plan["canonical_iri"] is None
    assert plan["needs_disambiguation"] is False


def test_unwraps_trigger_envelope(tmp_path, _restore_lookup):
    """Framework wraps input as {du_name: payload}; the step must unwrap."""
    fake = LookupResult(
        surface_form="EEEV",
        path="fast",
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_11021",
        canonical_label="Eastern equine encephalitis virus",
        canonical_ontology="ncbitaxon",
        confidence=1.0,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        synonyms=(),
        evidence="",
    )
    _restore_lookup.setattr(
        harmonized_resolve_step,
        "lookup_entity",
        _stub_lookup(fake),
    )
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"resolve_input": {"term": "EEEV", "index": "bvbrc_genome"}}))
    plan = out["plan"]
    assert plan["resolution_path"] == "fast"
    assert plan["term"] == "EEEV"


@pytest.mark.parametrize(
    "bad_input,expected_substr",
    [
        ({"index": "bvbrc_genome"}, "term"),
        ({"term": "CHIKV"}, "index"),
        ({"term": "", "index": "bvbrc_genome"}, "term"),
        ({"term": "CHIKV", "index": ""}, "index"),
        ({"term": 123, "index": "bvbrc_genome"}, "term"),
        ({"term": "CHIKV", "index": "bvbrc_genome", "entity_type": "not_a_type"}, "entity_type"),
    ],
)
def test_input_validation_loud(tmp_path, bad_input, expected_substr):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match=expected_substr):
        asyncio.run(step.process(bad_input))
