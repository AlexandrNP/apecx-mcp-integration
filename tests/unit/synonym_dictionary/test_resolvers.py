"""Unit tests for resolvers, with a fake OLS client.

The fake substitutes :meth:`get_term` and :meth:`search` with canned
responses, so the tests exercise the resolver's logic (anchor-mode vs
search-mode dispatch, disambiguation, unresolved fallback) without
hitting OLS.
"""

from __future__ import annotations

from typing import Any

import pytest
from apecx_integration.synonym_dictionary.enums import (
    OntologyName,
    ResolutionStatus,
)
from apecx_integration.synonym_dictionary.resolvers import (
    DiseaseResolver,
    PathogenResolver,
    VaccineResolver,
    normalize_iri,
)


class _FakeOLSClient:
    """Stub matching :class:`OLSClient`'s public surface used by resolvers."""

    def __init__(
        self,
        terms: dict[str, dict[str, Any]] | None = None,
        searches: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.terms = terms or {}
        self.searches = searches or {}

    async def get_term(
        self,
        ontology: OntologyName,
        iri: str,
    ) -> dict[str, Any] | None:
        return self.terms.get(iri)

    async def search(
        self,
        query: str,
        ontology: OntologyName,
        *,
        rows: int = 5,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        return self.searches.get((query, ontology.value), [])


# ---------- normalize_iri ----------


def test_normalize_iri_full_iri_passthrough() -> None:
    assert (
        normalize_iri(
            "http://purl.obolibrary.org/obo/NCBITaxon_37124",
            prefix="NCBITaxon_",
        )
        == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    )


def test_normalize_iri_bare_id() -> None:
    assert normalize_iri("VO_0000122", prefix="VO_") == "http://purl.obolibrary.org/obo/VO_0000122"


def test_normalize_iri_numeric_only() -> None:
    """VIOLIN's NCBI_Taxonomy_ID column is numeric (e.g. 37124).
    Combine with the prefix to make the IRI."""
    assert (
        normalize_iri(37124, prefix="NCBITaxon_")
        == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    )


def test_normalize_iri_handles_nan_and_none() -> None:
    assert normalize_iri(None, prefix="NCBITaxon_") is None
    assert normalize_iri(float("nan"), prefix="NCBITaxon_") is None
    assert normalize_iri("", prefix="NCBITaxon_") is None
    assert normalize_iri("nan", prefix="NCBITaxon_") is None


# ---------- PathogenResolver: anchor mode ----------


@pytest.mark.asyncio
async def test_pathogen_resolves_via_existing_taxonomy_id() -> None:
    fake = _FakeOLSClient(
        terms={
            "http://purl.obolibrary.org/obo/NCBITaxon_37124": {
                "iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
                "label": "Chikungunya virus",
                "synonyms": ["CHIKV"],
            }
        }
    )
    resolver = PathogenResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"NCBI_Taxonomy_ID": 37124, "Pathogen": "Chikungunya virus"})
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
    assert result.resolution_confidence == 1.0
    assert result.canonical_iri.endswith("NCBITaxon_37124")
    assert "CHIKV" in result.synonyms


@pytest.mark.asyncio
async def test_pathogen_resolves_via_bvbrc_implicit_taxon() -> None:
    """BV-BRC genome rows have ``genome_id`` like ``37124.6497`` — the
    leading dot-separated token is the implicit NCBITaxon."""
    fake = _FakeOLSClient(
        terms={
            "http://purl.obolibrary.org/obo/NCBITaxon_37124": {
                "iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
                "label": "Chikungunya virus",
                "synonyms": [],
            }
        }
    )
    resolver = PathogenResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve(
        {"genome_id": "37124.6497", "genome_name": "Chikungunya virus 181/25"}
    )
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
    assert result.canonical_iri.endswith("NCBITaxon_37124")


@pytest.mark.asyncio
async def test_pathogen_resolves_via_search_fallback() -> None:
    fake = _FakeOLSClient(
        searches={
            ("Yellow fever virus", "ncbitaxon"): [
                {
                    "iri": "http://purl.obolibrary.org/obo/NCBITaxon_11089",
                    "label": "Yellow fever virus",
                }
            ]
        },
        terms={
            "http://purl.obolibrary.org/obo/NCBITaxon_11089": {
                "iri": "http://purl.obolibrary.org/obo/NCBITaxon_11089",
                "label": "Yellow fever virus",
                "synonyms": ["YFV"],
            }
        },
    )
    resolver = PathogenResolver(fake, dictionary_version="test-v1")
    # No NCBI_Taxonomy_ID — must take the search path.
    result = await resolver.resolve({"Pathogen": "Yellow fever virus"})
    assert result.resolution_status == ResolutionStatus.OLS_EXACT
    assert result.resolution_confidence == 0.9
    assert result.canonical_iri.endswith("NCBITaxon_11089")


@pytest.mark.asyncio
async def test_pathogen_unresolved_when_search_returns_nothing() -> None:
    fake = _FakeOLSClient()  # no terms, no searches
    resolver = PathogenResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"Pathogen": "Made-up entity"})
    assert result.resolution_status == ResolutionStatus.UNRESOLVED
    assert result.canonical_iri is None


# ---------- VaccineResolver ----------


@pytest.mark.asyncio
async def test_vaccine_resolves_via_existing_vo_id() -> None:
    fake = _FakeOLSClient(
        terms={
            "http://purl.obolibrary.org/obo/VO_0000122": {
                "iri": "http://purl.obolibrary.org/obo/VO_0000122",
                "label": "Yellow fever 17D vaccine vector",
                "synonyms": ["YF17D"],
            }
        }
    )
    resolver = VaccineResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"Vaccine_Ontology_ID": "VO_0000122", "Vaccine_Name": "YF17D"})
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
    assert result.canonical_iri.endswith("VO_0000122")
    assert "YF17D" in result.synonyms


@pytest.mark.asyncio
async def test_vaccine_falls_through_to_search_when_id_missing() -> None:
    fake = _FakeOLSClient(
        searches={
            ("Hypothetical vaccine X", "vo"): [
                {
                    "iri": "http://purl.obolibrary.org/obo/VO_9999",
                    "label": "Hypothetical vaccine X",
                }
            ]
        },
        terms={
            "http://purl.obolibrary.org/obo/VO_9999": {
                "iri": "http://purl.obolibrary.org/obo/VO_9999",
                "label": "Hypothetical vaccine X",
                "synonyms": [],
            }
        },
    )
    resolver = VaccineResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve(
        {"Vaccine_Ontology_ID": None, "Vaccine_Name": "Hypothetical vaccine X"}
    )
    assert result.resolution_status == ResolutionStatus.OLS_EXACT


# ---------- DiseaseResolver ----------


@pytest.mark.asyncio
async def test_disease_resolver_search_only() -> None:
    fake = _FakeOLSClient(
        searches={
            ("yellow fever", "doid"): [
                {"iri": "http://purl.obolibrary.org/obo/DOID_8281", "label": "yellow fever"}
            ]
        },
        terms={
            "http://purl.obolibrary.org/obo/DOID_8281": {
                "iri": "http://purl.obolibrary.org/obo/DOID_8281",
                "label": "yellow fever",
                "synonyms": [],
            }
        },
    )
    resolver = DiseaseResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"Disease": "yellow fever"})
    assert result.resolution_status == ResolutionStatus.OLS_EXACT


@pytest.mark.asyncio
async def test_disease_resolver_unresolved_for_missing_field() -> None:
    fake = _FakeOLSClient()
    resolver = DiseaseResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"Disease": None})
    assert result.resolution_status == ResolutionStatus.UNRESOLVED
