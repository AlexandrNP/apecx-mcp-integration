"""Unit tests for the apecx-harvesters DataCite -> rag_synthesis
publication-dict bridge.

The adapter is the single seam between the harvester schema (rich
nested DataCite) and the synthesizer schema (flat dict consumable by
``_render_publications``). Adversarial probes have repeatedly found
that schema-bridge code is where silent-failure shapes accumulate;
this file pins down the contract.
"""

from __future__ import annotations

import pytest
from apecx_harvesters.loaders.base.model import (
    Creator,
    DataCite,
    Description,
    DescriptionType,
    Identifier,
    Publisher,
    Title,
    TitleType,
)

from apecx_integration.agents.rag_synthesis import datacite_to_publication


def _record(**overrides) -> DataCite:
    """A minimum valid DataCite with the schema's required fields,
    plus whatever overrides each test needs."""
    base = dict(
        identifier=Identifier(identifier="10.1234/abc", identifierType="DOI"),
        creators=[],
        titles=[Title(title="Untitled")],
        publisher=Publisher(name="Test Publisher"),
    )
    base.update(overrides)
    return DataCite(**base)


def test_minimum_record_yields_doi_and_required_fields():
    rec = _record()
    pub = datacite_to_publication(rec)
    assert pub == {
        "doi": "10.1234/abc",
        "title": "Untitled",
        "journal": "Test Publisher",
    }


def test_record_with_no_identifier_rejected():
    rec = _record(identifier=None)
    with pytest.raises(ValueError, match="no identifier"):
        datacite_to_publication(rec)


def test_record_with_non_doi_identifier_rejected():
    rec = _record(
        identifier=Identifier(identifier="ARK:/12345/x", identifierType="ARK"),
    )
    with pytest.raises(ValueError, match="not 'DOI'"):
        datacite_to_publication(rec)


def test_creators_with_given_and_family_concat_to_full_name():
    rec = _record(
        creators=[
            Creator(givenName="Ada", familyName="Lovelace"),
            Creator(givenName="Charles", familyName="Babbage"),
        ],
    )
    pub = datacite_to_publication(rec)
    assert pub["authors"] == ["Ada Lovelace", "Charles Babbage"]


def test_creators_with_only_name_falls_back_to_name():
    rec = _record(creators=[Creator(name="Anonymous Collective")])
    pub = datacite_to_publication(rec)
    assert pub["authors"] == ["Anonymous Collective"]


def test_creators_with_no_usable_name_skipped():
    """A Creator with only an affiliation / nameIdentifiers but no
    given/family/name is not citable as an author. Skip silently —
    the harvester can emit such records when the source API only
    supplied an affiliation."""
    rec = _record(creators=[Creator()])
    pub = datacite_to_publication(rec)
    assert "authors" not in pub


def test_primary_title_preferred_over_subtitle():
    """When DataCite carries multiple titles (primary + subtitle), the
    one with no titleType is the primary."""
    rec = _record(
        titles=[
            Title(title="A subtitle", titleType=TitleType.Subtitle),
            Title(title="The main title"),
        ],
    )
    pub = datacite_to_publication(rec)
    assert pub["title"] == "The main title"


def test_first_title_used_when_no_primary_marker():
    """If every title carries a titleType, fall back to the first."""
    rec = _record(
        titles=[
            Title(title="Translated", titleType=TitleType.TranslatedTitle),
            Title(title="Other", titleType=TitleType.AlternativeTitle),
        ],
    )
    pub = datacite_to_publication(rec)
    assert pub["title"] == "Translated"


def test_abstract_extracted_from_descriptions():
    rec = _record(
        descriptions=[
            Description(
                description="A non-abstract description.",
                descriptionType=DescriptionType.Methods,
            ),
            Description(
                description="The abstract paragraph.",
                descriptionType=DescriptionType.Abstract,
            ),
        ],
    )
    pub = datacite_to_publication(rec)
    assert pub["abstract"] == "The abstract paragraph."


def test_no_abstract_omits_field():
    rec = _record(
        descriptions=[
            Description(
                description="Methods only.",
                descriptionType=DescriptionType.Methods,
            ),
        ],
    )
    pub = datacite_to_publication(rec)
    assert "abstract" not in pub


def test_year_passed_through_as_string():
    rec = _record(publicationYear="2025")
    pub = datacite_to_publication(rec)
    assert pub["year"] == "2025"


def test_round_trip_with_synthesizer_publication_renderer():
    """End-of-bridge test: feed the adapted dict to the synthesizer's
    publication renderer and verify the citation token matches what
    the synthesizer would compute. This catches drift between the
    DOI shape this adapter produces and the regex pattern the
    renderer applies."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_publications,
    )
    rec = _record(
        identifier=Identifier(
            identifier="10.1038/s41586-2025-12345-6",
            identifierType="DOI",
        ),
        creators=[Creator(givenName="A", familyName="B")],
        publicationYear="2025",
        descriptions=[
            Description(
                description="Short abstract.",
                descriptionType=DescriptionType.Abstract,
            ),
        ],
    )
    pub = datacite_to_publication(rec)
    rendered, allowed = _render_publications([pub], cap=5, strict=True)
    assert allowed == {"[10.1038/s41586-2025-12345-6]"}
    assert "10.1038/s41586-2025-12345-6" in rendered
