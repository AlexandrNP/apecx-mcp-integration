"""Unit tests for DataCite field extraction from Globus harvested-corpus records.

Realistic fixtures mirror the actual shape returned by the aggregate Globus index
``e74bf12a`` (verified 2026-06-13): the title lives at ``titles[0].title`` and the
abstract at ``descriptions[0].description`` — NOT at flat ``title`` / ``abstract``
keys. The renderers used to read the flat keys, so every structural hit (and every
journal article) rendered as ``(untitled)`` with no content.

Integration coverage of the same code path: the live structural search in
``tests/integration/test_harmonized_search_aggregate_served_live.py`` and the
evidence-review e2e in ``tests/integration/test_viral_epitope_analysis.py``
exercise these extractors against real Globus records.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search._datacite import (
    datacite_description,
    datacite_identifiers,
    datacite_organisms,
    datacite_primary_id,
    datacite_subjects,
    datacite_taxon_iris,
    datacite_title,
)

# A real-shaped PDB record (fields trimmed) as returned by globus_client.search.
_PDB_CONTENT = {
    "titles": [{"title": "VLP structure of Chikungunya virus complexed with C37 Fab, 2f block."}],
    "descriptions": [
        {
            "descriptionType": "Abstract",
            "description": "Structure determined by Electron microscopy at 3.3 Å resolution.",
        }
    ],
    "subjects": [
        {"subject": "VIRUS LIKE PARTICLE"},
        {"subject": "chikungunya"},
        {"subject": "alphavirus"},
        {"subject": "antibody"},
    ],
}


def test_datacite_title_reads_nested_titles():
    assert datacite_title(_PDB_CONTENT) == (
        "VLP structure of Chikungunya virus complexed with C37 Fab, 2f block."
    )


def test_datacite_description_reads_nested_descriptions():
    assert datacite_description(_PDB_CONTENT) == (
        "Structure determined by Electron microscopy at 3.3 Å resolution."
    )


def test_datacite_subjects_dedup_and_cap():
    assert datacite_subjects(_PDB_CONTENT) == [
        "VIRUS LIKE PARTICLE",
        "chikungunya",
        "alphavirus",
        "antibody",
    ]
    assert datacite_subjects(_PDB_CONTENT, limit=2) == ["VIRUS LIKE PARTICLE", "chikungunya"]


def test_flat_key_fallback_for_normalized_records():
    """A harvester-normalized record with flat keys still resolves (backward-compat)."""
    flat = {"title": "Flat Title", "abstract": "Flat abstract."}
    assert datacite_title(flat) == "Flat Title"
    assert datacite_description(flat) == "Flat abstract."


def test_missing_or_malformed_content_returns_none_not_crash():
    for bad in (None, {}, {"titles": []}, {"titles": "nope"}, {"titles": [{}]}, "string"):
        assert datacite_title(bad) is None
        assert datacite_description(bad) is None
        assert datacite_subjects(bad) == []


def test_empty_subject_strings_skipped():
    content = {"subjects": [{"subject": ""}, {"subject": "real"}, {"nope": "x"}]}
    assert datacite_subjects(content) == ["real"]


# Real-shaped PDB record with the organism on each polymer entity (E3-2.1). 9IXA-like:
# the antigen (CHIKV) leads, a bound Fab (Homo sapiens) follows.
_PDB_WITH_ORGANISMS = {
    "pdb": {
        "polymer_entities": [
            {"scientific_name": "Chikungunya virus"},
            {"scientific_name": "Homo sapiens"},
            {"scientific_name": "Chikungunya virus"},  # duplicate chain
        ]
    }
}


def test_datacite_organisms_reads_scientific_names_deduped_order_preserved():
    assert datacite_organisms(_PDB_WITH_ORGANISMS) == ["Chikungunya virus", "Homo sapiens"]
    # CC-1: a real PDB record yields >=1 organism.
    assert len(datacite_organisms(_PDB_WITH_ORGANISMS)) >= 1


def test_datacite_organisms_empty_for_emdb_and_malformed():
    # EMDB records carry no pdb.polymer_entities -> [] (organism only in title/desc).
    for bad in (None, {}, "string", {"pdb": "nope"}, {"pdb": {"polymer_entities": [{}]}}):
        assert datacite_organisms(bad) == []


# --------------------------- object identifiers (P0 fix) ---------------------------

# Real shape (verified live 2026-06-16): alternateIdentifiers is a list of
# {alternateIdentifier, alternateIdentifierType}; DOIs would live in relatedIdentifiers.
_GENOME_CONTENT = {
    "titles": [{"title": "Chikungunya virus CHIKV/Homo sapiens/NIC/1800.1D/2014"}],
    "alternateIdentifiers": [
        {"alternateIdentifier": "37124", "alternateIdentifierType": "NCBI-Taxonomy"},
        {"alternateIdentifier": "KY703959", "alternateIdentifierType": "GenBank"},
        {"alternateIdentifier": "37124.51", "alternateIdentifierType": "BVBRC-Genome"},
    ],
    "subjects": [
        {
            "subject": "Chikungunya virus",
            "valueUri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        },
    ],
}
_STRUCT_CONTENT = {
    "alternateIdentifiers": [
        {"alternateIdentifier": "37124", "alternateIdentifierType": "NCBI-Taxonomy"},
        {"alternateIdentifier": "7H6J", "alternateIdentifierType": "PDB"},
        # BV-BRC doubles UniProt as "X;X" — must split + dedupe.
        {"alternateIdentifier": "Q1H8W5;Q1H8W5", "alternateIdentifierType": "UniProt"},
    ],
    "relatedIdentifiers": [
        {"relatedIdentifier": "10.1016/j.chom.2020.07.008", "relatedIdentifierType": "DOI"},
    ],
}


def test_datacite_identifiers_groups_by_type_and_splits_doubled():
    ids = datacite_identifiers(_GENOME_CONTENT)
    assert ids["GenBank"] == ["KY703959"]
    assert ids["BVBRC-Genome"] == ["37124.51"]
    assert ids["NCBI-Taxonomy"] == ["37124"]
    ids2 = datacite_identifiers(_STRUCT_CONTENT)
    assert ids2["PDB"] == ["7H6J"]
    assert ids2["UniProt"] == ["Q1H8W5"]  # ";"-doubled value collapsed
    assert ids2["DOI"] == ["10.1016/j.chom.2020.07.008"]  # from relatedIdentifiers


def test_datacite_primary_id_precedence_never_taxonomy():
    # structure → PDB wins; genome → GenBank wins; taxonomy is NEVER primary.
    assert datacite_primary_id(_STRUCT_CONTENT) == "PDB:7H6J"
    assert datacite_primary_id(_GENOME_CONTENT) == "GenBank:KY703959"
    # a record carrying ONLY a taxon id has no citable object → None
    assert (
        datacite_primary_id(
            {
                "alternateIdentifiers": [
                    {"alternateIdentifier": "9606", "alternateIdentifierType": "NCBI-Taxonomy"}
                ]
            }
        )
        is None
    )
    assert datacite_primary_id(None) is None and datacite_primary_id({}) is None


def test_datacite_taxon_iris():
    assert datacite_taxon_iris(_GENOME_CONTENT) == [
        "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    ]
    assert datacite_taxon_iris({}) == [] and datacite_taxon_iris(None) == []
