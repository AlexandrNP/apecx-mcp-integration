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
    datacite_organisms,
    datacite_subjects,
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
