"""Regression test for the literature-leg abstract bug.

``GlobusLiteratureSearchStep._hit_to_publication`` used to hardcode ``abstract=""``,
silently dropping the paper's real abstract from every Globus literature hit. The fix
reads it via ``datacite_description(content)`` (DataCite ``descriptions[0].description``).
This is a pure projection over a fixture hit dict — no network.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.composition.steps.globus_literature_search_step import (
    GlobusLiteratureSearchStep,
)


def _step(tmp_path: Path) -> GlobusLiteratureSearchStep:
    p = tmp_path / "globus_lit.yml"
    p.write_text("name: globus_lit_abstract_test\n")
    return GlobusLiteratureSearchStep.from_config(str(p))


def test_hit_to_publication_populates_abstract_from_datacite_description(tmp_path):
    hit = {
        "subject": "pubmed:12345678",
        "content": {
            "titles": [{"title": "T"}],
            "descriptions": [{"description": "REAL ABSTRACT TEXT"}],
            "alternateIdentifiers": [
                {"alternateIdentifierType": "PMID", "alternateIdentifier": "12345678"}
            ],
        },
        "score": None,
    }

    pub = _step(tmp_path)._hit_to_publication(hit)

    assert pub["abstract"] == "REAL ABSTRACT TEXT"
    assert pub["title"] == "T"
    assert pub["pmid"] == "12345678"


def test_hit_to_publication_abstract_empty_when_no_description(tmp_path):
    hit = {"subject": "pubmed:1", "content": {"titles": [{"title": "T"}]}, "score": None}

    pub = _step(tmp_path)._hit_to_publication(hit)

    assert pub["abstract"] == ""
