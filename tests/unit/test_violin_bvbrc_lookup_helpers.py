"""Unit tests for the stateless VIOLIN/BV-BRC lookup utility module.

These tests pin the behavior of ``_violin_bvbrc_lookup`` extracted from
``VIOLINBVBRCContextStep``. The tests use real CSV/TSV file fixtures
written to tmp_path so the test is hermetic — no APECX_DATA_ROOT, no
workspace data dir required.

Why these tests
---------------
The functions are now called from TWO call sites — the step class
AND the synthesis assembly step. A regression in either call site
would surface here independent of the workflow-level integration
tests, which is the right blast radius for unit tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.steps._violin_bvbrc_lookup import (
    lookup_bvbrc,
    lookup_violin,
)


@pytest.fixture
def violin_dir(tmp_path: Path) -> Path:
    """Build a minimal pair of VIOLIN CSVs in tmp_path."""
    pathogen_csv = tmp_path / "Pathogen_Information.csv"
    pathogen_csv.write_text(
        "id,Pathogen,NCBI_Taxonomy_ID\n"
        "1,Eastern equine encephalitis virus,11036\n"
        "2,SARS-CoV-2,2697049\n"
        "3,Ebola virus,186538\n"
    )
    vaccine_csv = tmp_path / "Vaccine_Information.csv"
    vaccine_csv.write_text(
        "id,Vaccine_Name,Vaccine_Ontology_ID\n"
        "10,EEEV inactivated vaccine,VO_0011001\n"
        "11,COVID-19 mRNA vaccine,VO_0010002\n"
    )
    return tmp_path


@pytest.fixture
def bvbrc_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a minimal BV-BRC alphavirus genomes TSV."""
    d = tmp_path_factory.mktemp("bvbrc")
    tsv = d / "alphavirus_genomes.tsv"
    tsv.write_text(
        "genome.genome_id\tgenome.genome_name\n"
        "11036.7\tEastern equine encephalitis virus strain X\n"
        "11036.8\tEastern equine encephalitis virus strain Y\n"
        "37124.1\tWestern equine encephalitis virus\n"
    )
    return d


def test_lookup_violin_eeev_matches_vaccine_not_pathogen(violin_dir):
    """Substring match: "EEEV" → fixture's pathogen row spells out
    "Eastern equine encephalitis virus" (no "EEEV" substring), but
    the vaccine row "EEEV inactivated vaccine" DOES contain it. So a
    pathogen-typed query that hits the vaccine row gets the entity
    type preserved (not overridden — "pathogen" is explicit, not
    "unknown")."""
    out = lookup_violin([("EEEV", "pathogen")], violin_dir, max_results=10)
    assert len(out) == 1
    hit = out[0]
    assert hit["source"] == "VIOLIN_Vaccine_Information"
    # Entity type was explicit "pathogen" — vaccine override only kicks
    # in for "unknown" types (verified by separate test below).
    assert hit["entity_type"] == "pathogen"
    assert hit["canonical_term"] == "VO_0011001"


def test_lookup_violin_substring_match(violin_dir):
    out = lookup_violin([("encephalitis", "pathogen")], violin_dir, max_results=10)
    assert len(out) == 1
    hit = out[0]
    assert hit["synonym_id"] == "VIOLIN_pathogen_1"
    assert hit["canonical_term"] == "11036"
    assert hit["entity_type"] == "pathogen"
    assert hit["source"] == "VIOLIN_Pathogen_Information"


def test_lookup_violin_falls_back_to_pathogen_name_when_taxon_id_missing(tmp_path):
    """Empty ``NCBI_Taxonomy_ID`` → canonical_term falls back to the
    Pathogen display name (the strict_input_validation gate would
    otherwise reject the synthesizer mapping)."""
    pathogen_csv = tmp_path / "Pathogen_Information.csv"
    pathogen_csv.write_text("id,Pathogen,NCBI_Taxonomy_ID\n4,Lab strain Foo,\n")
    out = lookup_violin([("foo", "pathogen")], tmp_path, max_results=10)
    assert len(out) == 1
    # Falls back to display name, not empty string.
    assert out[0]["canonical_term"] == "Lab strain Foo"


def test_lookup_violin_vaccine_match_overrides_unknown_type(violin_dir):
    """A query that hits the vaccine CSV gets entity_type=vaccine even
    when the input said "unknown" — vaccine matches override unknown
    (but not explicit pathogen/gene types)."""
    out = lookup_violin([("mRNA", "unknown")], violin_dir, max_results=10)
    assert len(out) == 1
    assert out[0]["source"] == "VIOLIN_Vaccine_Information"
    assert out[0]["entity_type"] == "vaccine"


def test_lookup_violin_max_results_cap(violin_dir):
    out = lookup_violin([("encephalitis", "pathogen")], violin_dir, max_results=0)
    assert out == []


def test_lookup_violin_empty_terms_short_circuits(violin_dir):
    out = lookup_violin([], violin_dir, max_results=10)
    assert out == []


def test_lookup_violin_missing_dir_returns_empty(tmp_path):
    """No CSVs at the path → empty list, log WARNING, no crash."""
    out = lookup_violin([("EEEV", "pathogen")], tmp_path / "nope", max_results=10)
    assert out == []


def test_lookup_violin_corrupted_csv_logged_and_skipped(tmp_path, caplog):
    """Malformed CSV → warning, empty result for that file (graceful
    degradation contract)."""
    bad_csv = tmp_path / "Pathogen_Information.csv"
    bad_csv.write_bytes(b"\x00\x01\x02not a real csv")
    # Don't include vaccine CSV → only one source available.
    with caplog.at_level("WARNING"):
        out = lookup_violin([("EEEV", "pathogen")], tmp_path, max_results=10)
    assert out == []
    # The warning surfaces the corruption.
    assert any("failed reading" in r.message or "missing" in r.message for r in caplog.records)


def test_lookup_bvbrc_substring_match(bvbrc_dir):
    out = lookup_bvbrc([("encephalitis", "pathogen")], bvbrc_dir, max_results=10)
    assert len(out) == 3  # all three matches contain "encephalitis"
    ids = [g["genome_id"] for g in out]
    assert "11036.7" in ids


def test_lookup_bvbrc_dedupes_by_genome_id(tmp_path):
    """Two terms both matching the same genome → return it once, not twice."""
    d = tmp_path
    (d / "alphavirus_genomes.tsv").write_text(
        "genome.genome_id\tgenome.genome_name\n11036.7\tEEEV strain X\n"
    )
    out = lookup_bvbrc(
        [("EEEV", "pathogen"), ("strain", "pathogen")],
        d,
        max_results=10,
    )
    assert len(out) == 1


def test_lookup_bvbrc_max_results_cap(bvbrc_dir):
    out = lookup_bvbrc(
        [("encephalitis", "pathogen")],
        bvbrc_dir,
        max_results=2,
    )
    assert len(out) == 2


def test_lookup_bvbrc_missing_tsv_returns_empty(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        out = lookup_bvbrc([("X", "p")], tmp_path / "nope", max_results=10)
    assert out == []
    assert any("BV-BRC TSV not found" in r.message for r in caplog.records)


def test_lookup_bvbrc_missing_genome_name_column_returns_empty(tmp_path, caplog):
    """Wrong column shape → log WARNING, return empty (don't crash)."""
    d = tmp_path
    (d / "alphavirus_genomes.tsv").write_text("wrong.column\nfoo\n")
    with caplog.at_level("WARNING"):
        out = lookup_bvbrc([("X", "p")], d, max_results=10)
    assert out == []
    assert any("genome.genome_name" in r.message for r in caplog.records)


def test_owner_name_appears_in_log_messages(tmp_path, caplog):
    """``owner_name`` flows into log prefixes so an operator can
    correlate WARNINGs back to the calling step."""
    with caplog.at_level("WARNING"):
        lookup_bvbrc(
            [("X", "p")],
            tmp_path / "nope",
            max_results=10,
            owner_name="my_caller_step",
        )
    assert any("my_caller_step:" in r.message for r in caplog.records)
