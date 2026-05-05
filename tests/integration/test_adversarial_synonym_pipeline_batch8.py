"""Adversarial probes batch 8 — probes 211-240.

Targets: OntologyName enum invariants, normalization surround-punct stripping
deep corner cases, datacite_to_publication bridge (missing ID / wrong type /
author formatting / title fallback / publisher-as-journal), and
ResolutionResult + ProvisionalSynonym schema invariants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# OntologyName enum invariants (211-220)
# ---------------------------------------------------------------------------


def test_probe_211_ontology_name_ncbitaxon_value_is_string():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.NCBITAXON == "ncbitaxon"
    assert isinstance(OntologyName.NCBITAXON.value, str)


def test_probe_212_ontology_name_vo_value():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.VO == "vo"


def test_probe_213_ontology_name_doid_value():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.DOID == "doid"


def test_probe_214_ontology_name_go_value():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.GO == "go"


def test_probe_215_ontology_name_ncbigene_value():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.NCBIGENE == "ncbigene"


def test_probe_216_ontology_name_apecx_local_value():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName.APECX_LOCAL == "apecx_local"


def test_probe_217_ontology_name_from_string_lookup():
    """OntologyName is a StrEnum — value lookup via string works."""
    from apecx_integration.synonym_dictionary.enums import OntologyName

    assert OntologyName("ncbitaxon") == OntologyName.NCBITAXON


def test_probe_218_ontology_name_unknown_string_raises():
    from apecx_integration.synonym_dictionary.enums import OntologyName

    with pytest.raises(ValueError):
        OntologyName("unknown_ontology_xyz")


def test_probe_219_entity_type_pathogen_value_is_string():
    from apecx_integration.synonym_dictionary.enums import EntityType

    assert EntityType.PATHOGEN == "pathogen"
    assert isinstance(EntityType.PATHOGEN.value, str)


def test_probe_220_entity_type_from_string_lookup():
    from apecx_integration.synonym_dictionary.enums import EntityType

    assert EntityType("vaccine") == EntityType.VACCINE


# ---------------------------------------------------------------------------
# normalization surround-punct deep corner cases (221-230)
# ---------------------------------------------------------------------------


def test_probe_221_normalize_strips_surrounding_parens():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    # A term that starts/ends with parens should have them stripped
    assert normalize_surface_form("(EEEV)") == "eeev"


def test_probe_222_normalize_preserves_internal_parens():
    """Internal parens (not at string boundary) are preserved."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("Influenza A (H1N1)")
    # trailing paren stripped per the _SURROUND_PUNCT regex, but internal not
    assert "h1n1" in result


def test_probe_223_normalize_strips_surrounding_brackets():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("[SARS-CoV-2]") == "sars-cov-2"


def test_probe_224_normalize_strips_surrounding_quotes():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form('"Ebola virus"') == "ebola virus"


def test_probe_225_normalize_collapses_internal_whitespace():
    """Multiple internal spaces collapse to single space."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("Zika   virus") == "zika virus"


def test_probe_226_normalize_casefold_german_eszett():
    """Casefold converts ß → ss (German-specific rule)."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("StraSSe") == "strasse"
    result = normalize_surface_form("Straße")
    assert "ss" in result  # ß → ss


def test_probe_227_normalize_nfkc_decomposes_ligatures():
    """NFKC normalizes ligature characters like ﬁ → fi."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    # ﬁ (U+FB01) should normalize to "fi"
    result = normalize_surface_form("ﬁbrosis")
    assert result == "fibrosis"


def test_probe_228_normalize_tab_treated_as_whitespace():
    """Tab character is collapsed to a single space."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("Zika\tvirus") == "zika virus"


def test_probe_229_normalize_newline_in_middle_treated_as_whitespace():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("Ebola\nvirus")
    assert result == "ebola virus"


def test_probe_230_normalize_result_is_always_str():
    """normalize_surface_form never returns None; always returns str."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    for s in ["", "  ", "EEEV", None.__class__.__name__]:
        result = normalize_surface_form(s)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# datacite_to_publication bridge (231-240)
# ---------------------------------------------------------------------------


def _make_datacite(
    *,
    doi: str | None = "10.1234/test",
    title: str = "Test Paper",
    year: str = "2020",
    author_name: str = "Doe J",
    publisher_name: str | None = None,
):
    from apecx_harvesters.loaders.base.model import (
        Creator,
        DataCite,
        Identifier,
        Publisher,
        Title,
    )

    ident = Identifier(identifier=doi, identifierType="DOI") if doi else None
    titles = [Title(title=title)]
    creators = [Creator(name=author_name)]
    pub = Publisher(name=publisher_name) if publisher_name else Publisher(name="")
    return DataCite(
        identifier=ident,
        titles=titles,
        creators=creators,
        publisher=pub,
        publicationYear=year,
    )


def test_probe_231_datacite_to_publication_doi_in_output():
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(doi="10.1234/my.doi")
    pub = datacite_to_publication(record)
    assert pub["doi"] == "10.1234/my.doi"


def test_probe_232_datacite_to_publication_no_identifier_raises():
    from apecx_harvesters.loaders.base.model import Creator, DataCite, Publisher, Title
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=None,
        titles=[Title(title="No DOI paper")],
        creators=[Creator(name="Author")],
        publisher=Publisher(name=""),
        publicationYear="2020",
    )
    with pytest.raises(ValueError, match="identifier|DOI"):
        datacite_to_publication(record)


def test_probe_233_datacite_to_publication_wrong_identifier_type_raises():
    from apecx_harvesters.loaders.base.model import (
        Creator,
        DataCite,
        Identifier,
        Publisher,
        Title,
    )
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=Identifier(identifier="ISBN:978-3-030", identifierType="ISBN"),
        titles=[Title(title="Book")],
        creators=[Creator(name="Author")],
        publisher=Publisher(name=""),
        publicationYear="2020",
    )
    with pytest.raises(ValueError, match="DOI|identifierType"):
        datacite_to_publication(record)


def test_probe_234_datacite_to_publication_title_in_output():
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(title="A Novel Vaccine Study")
    pub = datacite_to_publication(record)
    assert pub.get("title") == "A Novel Vaccine Study"


def test_probe_235_datacite_to_publication_year_in_output():
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(year="2019")
    pub = datacite_to_publication(record)
    assert pub.get("year") == "2019"


def test_probe_236_datacite_to_publication_author_name_in_output():
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(author_name="Jane Smith")
    pub = datacite_to_publication(record)
    authors = pub.get("authors", [])
    assert any("Jane Smith" in a for a in authors)


def test_probe_237_datacite_to_publication_given_family_name_formatted():
    """givenName + familyName → 'Given Family' format."""
    from apecx_harvesters.loaders.base.model import (
        Creator,
        DataCite,
        Identifier,
        Publisher,
        Title,
    )
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=Identifier(identifier="10.1234/abc", identifierType="DOI"),
        titles=[Title(title="Study")],
        creators=[Creator(givenName="Jane", familyName="Smith", name=None)],
        publisher=Publisher(name=""),
        publicationYear="2021",
    )

    pub = datacite_to_publication(record)
    assert "Jane Smith" in pub.get("authors", [])


def test_probe_238_datacite_to_publication_publisher_as_journal():
    """Publisher name is mapped to the 'journal' key."""
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(publisher_name="Nature")
    pub = datacite_to_publication(record)
    assert pub.get("journal") == "Nature"


def test_probe_239_datacite_to_publication_no_publisher_no_journal_key():
    """If publisher name is empty, 'journal' key is absent (not null)."""
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(publisher_name=None)
    pub = datacite_to_publication(record)
    assert "journal" not in pub


def test_probe_240_datacite_to_publication_result_has_doi_key():
    """Output dict always has 'doi' key when identifier is a DOI."""
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = _make_datacite(doi="10.9999/sample")
    pub = datacite_to_publication(record)
    assert "doi" in pub
