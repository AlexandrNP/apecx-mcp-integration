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
    GeneResolver,
    PathogenResolver,
    VaccineResolver,
    _build_ncbigene_iri,
    _extract_gene_synonyms,
    _gene_label_from_name,
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


def test_normalize_iri_float_string_from_pandas() -> None:
    """Regression: pandas reads integer columns as numpy.float64.
    str(numpy.float64(10298.0)) == '10298.0' which isdigit() == False.
    normalize_iri must handle float-integer strings correctly."""
    # Float-string form that pandas produces for integer columns
    assert (
        normalize_iri("10298.0", prefix="NCBITaxon_")
        == "http://purl.obolibrary.org/obo/NCBITaxon_10298"
    )
    # The actual float value from numpy
    import numpy as np

    val = np.float64(10298.0)
    assert (
        normalize_iri(val, prefix="NCBITaxon_") == "http://purl.obolibrary.org/obo/NCBITaxon_10298"
    )
    # Negative float should not produce a valid IRI
    assert normalize_iri(-1.0, prefix="NCBITaxon_") is None
    # Non-integer float should return None
    assert normalize_iri(10298.5, prefix="NCBITaxon_") is None


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


# ---------- _build_ncbigene_iri helpers ----------


def test_build_ncbigene_iri_integer() -> None:
    assert _build_ncbigene_iri(15978) == "http://identifiers.org/ncbigene/15978"


def test_build_ncbigene_iri_float_string_from_pandas() -> None:
    assert _build_ncbigene_iri("15978.0") == "http://identifiers.org/ncbigene/15978"


def test_build_ncbigene_iri_none_and_nan() -> None:
    assert _build_ncbigene_iri(None) is None
    assert _build_ncbigene_iri(float("nan")) is None
    assert _build_ncbigene_iri("nan") is None
    assert _build_ncbigene_iri("") is None


def test_build_ncbigene_iri_full_url_passthrough() -> None:
    url = "http://identifiers.org/ncbigene/15978"
    assert _build_ncbigene_iri(url) == url


# ---------- _gene_label_from_name helpers ----------


def test_gene_label_from_symbol_long_pattern() -> None:
    assert _gene_label_from_name("Ifng (Interferon gamma)") == "Ifng"


def test_gene_label_from_organism_pattern() -> None:
    assert _gene_label_from_name("SodC from B. abortus strain 2308") == "SodC"


def test_gene_label_bare_symbol() -> None:
    assert _gene_label_from_name("IglC") == "IglC"


# ---------- _extract_gene_synonyms helpers ----------


def test_extract_gene_synonyms_parens_pattern() -> None:
    syns = _extract_gene_synonyms("Ifng (Interferon gamma)")
    assert "Ifng" in syns
    assert "Interferon gamma" in syns
    assert "Ifng (Interferon gamma)" in syns


def test_extract_gene_synonyms_from_pattern() -> None:
    syns = _extract_gene_synonyms("SodC from B. abortus strain 2308")
    assert "SodC" in syns
    assert "SodC from B. abortus strain 2308" in syns


def test_extract_gene_synonyms_bare_symbol() -> None:
    syns = _extract_gene_synonyms("IglC")
    assert "IglC" in syns


def test_extract_gene_synonyms_empty() -> None:
    assert _extract_gene_synonyms("") == ()
    assert _extract_gene_synonyms("   ") == ()


# ---------- GeneResolver ----------


@pytest.mark.asyncio
async def test_gene_resolver_resolves_via_ncbi_gene_id() -> None:
    """Covered by tests/integration/test_gene_dictionary.py::test_gene_build_anchor_mode_confidence."""
    fake = _FakeOLSClient()  # never called by GeneResolver
    resolver = GeneResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve(
        {"NCBI_Gene_ID": "15978.0", "Gene_Name": "Ifng (Interferon gamma)"}
    )
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
    assert result.resolution_confidence == 1.0
    assert result.canonical_iri == "http://identifiers.org/ncbigene/15978"
    assert result.canonical_label == "Ifng"
    assert "Interferon gamma" in result.synonyms
    assert "Ifng (Interferon gamma)" in result.synonyms


@pytest.mark.asyncio
async def test_gene_resolver_handles_bare_integer_id() -> None:
    fake = _FakeOLSClient()
    resolver = GeneResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"NCBI_Gene_ID": 3827840, "Gene_Name": "SodC"})
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
    assert result.canonical_iri == "http://identifiers.org/ncbigene/3827840"


@pytest.mark.asyncio
async def test_gene_resolver_unresolved_when_no_ncbi_gene_id() -> None:
    fake = _FakeOLSClient()
    resolver = GeneResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"NCBI_Gene_ID": None, "Gene_Name": "some gene"})
    assert result.resolution_status == ResolutionStatus.UNRESOLVED
    assert result.canonical_iri is None


@pytest.mark.asyncio
async def test_gene_resolver_unresolved_for_nan_id() -> None:
    fake = _FakeOLSClient()
    resolver = GeneResolver(fake, dictionary_version="test-v1")
    result = await resolver.resolve({"NCBI_Gene_ID": float("nan"), "Gene_Name": "some gene"})
    assert result.resolution_status == ResolutionStatus.UNRESOLVED


@pytest.mark.asyncio
async def test_gene_resolver_does_not_call_ols() -> None:
    """GeneResolver must never call OLS — it builds IRIs from source data only."""

    class _AssertingOLSClient(_FakeOLSClient):
        async def get_term(self, ontology, iri):
            raise AssertionError("GeneResolver called get_term — should not use OLS")

        async def search(self, query, ontology, *, rows=5, exact=False):
            raise AssertionError("GeneResolver called search — should not use OLS")

    resolver = GeneResolver(_AssertingOLSClient(), dictionary_version="test-v1")
    result = await resolver.resolve({"NCBI_Gene_ID": "15978.0", "Gene_Name": "Ifng"})
    # If we get here without AssertionError, OLS was not called.
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED
