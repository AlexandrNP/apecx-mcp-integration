"""Unit tests for the synonym-dictionary Pydantic schemas.

Mostly verifying the ``extra='forbid'`` and frozen guarantees, plus
the confidence-bounds validation.  These are the contract tests that
guard the on-disk artifact's shape against accidental drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from apecx_integration.synonym_dictionary import (
    BuildManifest,
    DictionaryEntry,
    EntityType,
    OntologyName,
    ProvisionalSynonym,
    ResolutionResult,
    ResolutionStatus,
)
from pydantic import ValidationError

# ---------- ResolutionResult ----------


def test_resolution_result_id_anchored() -> None:
    r = ResolutionResult(
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        canonical_ontology=OntologyName.NCBITAXON,
        synonyms=("CHIKV", "Chikungunya"),
        resolution_status=ResolutionStatus.ID_ANCHORED,
        resolution_confidence=1.0,
        dictionary_version="2026-05-01.1",
    )
    assert r.canonical_iri.endswith("NCBITaxon_37124")
    assert r.resolution_confidence == 1.0


def test_resolution_result_unresolved() -> None:
    """Unresolved rows must have None canonical fields and confidence 0.0."""
    r = ResolutionResult(
        canonical_iri=None,
        canonical_label=None,
        canonical_ontology=None,
        synonyms=(),
        resolution_status=ResolutionStatus.UNRESOLVED,
        resolution_confidence=0.0,
        dictionary_version="2026-05-01.1",
    )
    assert r.canonical_iri is None
    assert r.resolution_status == ResolutionStatus.UNRESOLVED


def test_resolution_result_rejects_extra_fields() -> None:
    """extra='forbid' must fail loudly on unknown keys (workspace memory rule)."""
    with pytest.raises(ValidationError):
        # Note: ``cannonical_iri`` is an intentional typo verifying that
        # extra='forbid' raises rather than silently using a default.
        ResolutionResult(
            canonical_iri="x",
            canonical_label="y",
            canonical_ontology=OntologyName.VO,
            synonyms=(),
            resolution_status=ResolutionStatus.OLS_EXACT,
            resolution_confidence=0.9,
            dictionary_version="v1",
            cannonical_iri="typo",
        )


def test_resolution_result_confidence_bounds() -> None:
    base = {
        "canonical_iri": "x",
        "canonical_label": "y",
        "canonical_ontology": OntologyName.VO,
        "synonyms": (),
        "resolution_status": ResolutionStatus.OLS_EXACT,
        "dictionary_version": "v1",
    }
    with pytest.raises(ValidationError):
        ResolutionResult(**base, resolution_confidence=-0.01)
    with pytest.raises(ValidationError):
        ResolutionResult(**base, resolution_confidence=1.01)


def test_resolution_result_is_frozen() -> None:
    r = ResolutionResult(
        canonical_iri="x",
        canonical_label="y",
        canonical_ontology=OntologyName.VO,
        synonyms=(),
        resolution_status=ResolutionStatus.OLS_EXACT,
        resolution_confidence=0.9,
        dictionary_version="v1",
    )
    with pytest.raises(ValidationError):
        r.canonical_iri = "z"  # type: ignore[misc]


# ---------- DictionaryEntry ----------


def test_dictionary_entry_minimal() -> None:
    e = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        synonyms=("CHIKV",),
        ontology=OntologyName.NCBITAXON,
        ontology_version="ncbitaxon-2026-04-01",
        confidence=1.0,
        resolved_at=datetime.now(UTC),
    )
    assert e.entity_type == EntityType.PATHOGEN


def test_dictionary_entry_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="x",
            canonical_label="y",
            synonyms=(),
            ontology=OntologyName.NCBITAXON,
            ontology_version="v1",
            confidence=1.0,
            resolved_at=datetime.now(UTC),
            xtra_field="oops",
        )


# ---------- BuildManifest ----------


def test_build_manifest_minimal() -> None:
    m = BuildManifest(
        dictionary_version="2026-05-01.1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "2026-04-01", "vo": "2026-03-15"},
        record_counts_per_entity_type={
            EntityType.PATHOGEN: 217,
            EntityType.VACCINE: 3507,
        },
        unresolved_count=7,
        record_count_total=3724,
    )
    assert m.schema_version == "1.0.0"
    assert m.harvester_version is None  # local-CLI build, no harvester


def test_build_manifest_unresolved_count_non_negative() -> None:
    with pytest.raises(ValidationError):
        BuildManifest(
            dictionary_version="v1",
            built_at=datetime.now(UTC),
            ontology_versions={},
            record_counts_per_entity_type={},
            unresolved_count=-1,
            record_count_total=0,
        )


# ---------- ProvisionalSynonym ----------


def test_provisional_synonym_with_no_iri() -> None:
    """canonical_iri=None means 'user reports this surface form has no good
    match' — a separate signal worth recording."""
    p = ProvisionalSynonym(
        entity_type=EntityType.PATHOGEN,
        canonical_iri=None,
        surface_form="frobnicator",
        proposed_at=datetime.now(UTC),
        proposed_by="test_user_42",
        confidence=0.0,
    )
    assert p.canonical_iri is None
    assert p.confidence == 0.0


def test_provisional_synonym_full() -> None:
    p = ProvisionalSynonym(
        entity_type=EntityType.VACCINE,
        canonical_iri="http://purl.obolibrary.org/obo/VO_0000122",
        surface_form="YF17D",
        proposed_at=datetime.now(UTC),
        proposed_by="user_x",
        confidence=0.85,
        promotion_signals=("voter_a", "voter_b"),
    )
    assert len(p.promotion_signals) == 2
