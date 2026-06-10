"""SC-B3 + SC-B4 (2026-06-08) — unit tests for mined-observation ingest.

Exercises:
- ``mined_conflicts`` table DDL + write path (SC-B3).
- ``ingest_mined_observations`` against a synthetic dictionary
  fixture (SC-B4).
- The SC-A5b multi-IRI inverse path is preserved when the mined
  ingest creates new ambiguity captures.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
from apecx_integration.synonym_dictionary.mined_ingest import (
    ingest_mined_observations,
)
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
)
from apecx_integration.synonym_dictionary.sqlite_writer import (
    SQLiteDictionaryWriter,
)


def _write_test_dictionary(path: Path) -> None:
    """Write a minimal pathogen dictionary covering 4 virus taxa."""
    manifest = BuildManifest(
        dictionary_version="mined-ingest-test-v1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "test"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 4},
        unresolved_count=0,
        record_count_total=4,
    )
    entries = [
        ("NCBITaxon_11021", "Eastern equine encephalitis virus", ("EEEV",)),
        ("NCBITaxon_64320", "Zika virus", ("ZIKV",)),
        ("NCBITaxon_11250", "Human orthopneumovirus", ()),
        ("NCBITaxon_11246", "Bovine orthopneumovirus", ()),
    ]
    with SQLiteDictionaryWriter(path) as writer:
        for iri_tail, label, syns in entries:
            writer.write_entry(
                DictionaryEntry(
                    entity_type=EntityType.PATHOGEN,
                    canonical_iri=f"http://purl.obolibrary.org/obo/{iri_tail}",
                    canonical_label=label,
                    synonyms=syns,
                    ontology=OntologyName.NCBITAXON,
                    ontology_version="test",
                    source_records=(),
                    confidence=1.0,
                    resolved_at=datetime.now(UTC),
                )
            )
        writer.write_manifest(manifest)


def _write_mined_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_synonyms(conn: sqlite3.Connection, iri: str) -> list[str]:
    row = conn.execute(
        "SELECT synonyms_json FROM entries WHERE canonical_iri = ?", (iri,)
    ).fetchone()
    return json.loads(row[0]) if row else []


def _read_inverse(conn: sqlite3.Connection, normalized: str) -> str | None:
    row = conn.execute(
        "SELECT canonical_iri FROM inverse_index "
        "WHERE entity_type = 'pathogen' AND surface_form_normalized = ?",
        (normalized,),
    ).fetchone()
    return row[0] if row else None


def test_ingest_adds_new_synonym_to_existing_entry(tmp_path: Path) -> None:
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            {
                "surface_form": "EEEVirus",
                "surface_form_normalized": "eeevirus",
                "taxon_id": 11021,
                "source": "violin_pathogen",
                "source_count": 1,
            },
        ],
    )

    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)

    assert summary.rows_read == 1
    assert summary.entries_touched == 1
    assert summary.synonyms_added == 1
    assert summary.inverse_writes == 1
    assert summary.missing_entries == 0

    conn = sqlite3.connect(db)
    try:
        syns = _read_synonyms(conn, "http://purl.obolibrary.org/obo/NCBITaxon_11021")
        assert "EEEVirus" in syns
        # Original "EEEV" still present (dedup).
        assert "EEEV" in syns
        assert _read_inverse(conn, "eeevirus") == ("http://purl.obolibrary.org/obo/NCBITaxon_11021")
    finally:
        conn.close()


def test_ingest_dedupes_existing_synonyms(tmp_path: Path) -> None:
    """Already-known synonyms are not re-added."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            {
                "surface_form": "EEEV",  # already a synonym of 11021
                "surface_form_normalized": "eeev",
                "taxon_id": 11021,
                "source": "violin_pathogen",
                "source_count": 1,
            },
        ],
    )
    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)
    assert summary.synonyms_added == 0
    conn = sqlite3.connect(db)
    try:
        syns = _read_synonyms(conn, "http://purl.obolibrary.org/obo/NCBITaxon_11021")
        # Single "EEEV" entry — no duplication.
        assert syns.count("EEEV") == 1
    finally:
        conn.close()


def test_ingest_handles_missing_entry_gracefully(tmp_path: Path) -> None:
    """Mined observation against a taxon NOT in the dictionary is counted, not crashed."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            {
                "surface_form": "Bacterium X",
                "surface_form_normalized": "bacterium x",
                "taxon_id": 99999,  # not in dict
                "source": "bvbrc_genome",
                "source_count": 1,
            },
        ],
    )
    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)
    assert summary.missing_entries == 1
    assert summary.entries_touched == 0


def test_ingest_surfaces_conflicts_to_mined_conflicts_table(
    tmp_path: Path,
) -> None:
    """SC-B3: same surface → 2 distinct taxa → both rows in mined_conflicts
    (preserving source provenance)."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            # Two sources see "RSV" mapped to two different taxa.
            {
                "surface_form": "RSV",
                "surface_form_normalized": "rsv",
                "taxon_id": 11250,
                "source": "violin_pathogen",
                "source_count": 1,
            },
            {
                "surface_form": "RSV",
                "surface_form_normalized": "rsv",
                "taxon_id": 11246,
                "source": "bvbrc_genome",
                "source_count": 1,
            },
        ],
    )
    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)
    assert summary.mined_conflicts_written >= 2

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT surface_form_normalized, candidate_taxon_id, conflict_source "
            "FROM mined_conflicts ORDER BY candidate_taxon_id"
        ).fetchall()
        assert len(rows) == 2
        surface_taxon_source = {(r[0], r[1], r[2]) for r in rows}
        assert ("rsv", 11246, "bvbrc_genome") in surface_taxon_source
        assert ("rsv", 11250, "violin_pathogen") in surface_taxon_source
    finally:
        conn.close()


def test_ingest_preserves_sc_a5b_ambiguity_path(tmp_path: Path) -> None:
    """When mining writes a colliding inverse_index row, the SC-A5b
    ambiguity-capture path (writer.ambiguous_surface_forms) fires."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    # Add a pre-existing inverse row for "rsv" → 11250 (Human RSV).
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO inverse_index (entity_type, surface_form_normalized, "
        "canonical_iri) VALUES ('pathogen', 'rsv', ?)",
        ("http://purl.obolibrary.org/obo/NCBITaxon_11250",),
    )
    conn.commit()
    conn.close()

    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            {
                "surface_form": "RSV",
                "surface_form_normalized": "rsv",
                "taxon_id": 11246,  # different IRI
                "source": "bvbrc_genome",
                "source_count": 1,
            },
        ],
    )
    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)
    assert summary.new_ambiguity_captures >= 1

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT winning_canonical_iri, alternative_canonical_iri "
            "FROM ambiguous_surface_forms WHERE surface_form_normalized='rsv'"
        ).fetchall()
        # At least one row captures the prior 11250 as alternative.
        alts = {alt for (_, alt) in rows}
        assert "http://purl.obolibrary.org/obo/NCBITaxon_11250" in alts
    finally:
        conn.close()


def test_ingest_writes_no_synonym_on_empty_normalized(tmp_path: Path) -> None:
    """A surface that normalizes to empty string is silently dropped."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    mined = tmp_path / "mined.jsonl"
    _write_mined_jsonl(
        mined,
        [
            {
                "surface_form": "   ",
                "surface_form_normalized": "",
                "taxon_id": 11021,
                "source": "violin_pathogen",
                "source_count": 1,
            },
        ],
    )
    summary = ingest_mined_observations(dict_path=db, mined_jsonl=mined)
    assert summary.synonyms_added == 0


def test_ingest_raises_on_missing_inputs(tmp_path: Path) -> None:
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    with pytest.raises(FileNotFoundError):
        ingest_mined_observations(dict_path=db, mined_jsonl=tmp_path / "missing.jsonl")
    mined = tmp_path / "mined.jsonl"
    mined.write_text("")
    with pytest.raises(FileNotFoundError):
        ingest_mined_observations(dict_path=tmp_path / "nope.sqlite", mined_jsonl=mined)


def test_mined_conflicts_table_exists_after_writer_init(tmp_path: Path) -> None:
    """SC-B3 DDL: the table is created at writer construction time."""
    db = tmp_path / "dict.sqlite"
    _write_test_dictionary(db)
    conn = sqlite3.connect(db)
    try:
        # Should not raise.
        rows = conn.execute("SELECT COUNT(*) FROM mined_conflicts").fetchone()
        assert rows[0] == 0
    finally:
        conn.close()
