"""Smoke tests for the ``apecx-lookup`` CLI (SC-A4b, 2026-06-08).

Exercises the wiring (argparse → loader configuration → lookup_entity
→ stdout rendering) without re-testing the resolution logic, which is
covered exhaustively by ``tests/unit/synonym_dictionary/test_loader_lookup.py``.
The integration-test parity counterpart is the manual smoke run
recorded in the SC-A4b implementation log entry of
``apecx-harvesters-work/design/SYNONYM_COMPLETENESS_PLAN.md``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apecx_integration.cli.lookup import main
from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter


def _write_test_dictionary(path: Path) -> None:
    """Write a minimal dictionary with one unambiguous + one ambiguous surface form."""
    manifest = BuildManifest(
        dictionary_version="cli-smoke-v1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "2026-04-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 3},
        unresolved_count=0,
        record_count_total=3,
    )
    with SQLiteDictionaryWriter(path) as writer:
        # Unambiguous: only ZIKV → 64320
        writer.write_entry(
            DictionaryEntry(
                entity_type=EntityType.PATHOGEN,
                canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_64320",
                canonical_label="Zika virus",
                synonyms=("ZIKV",),
                ontology=OntologyName.NCBITAXON,
                ontology_version="2026-04-01",
                source_records=("ncbitaxon.species.64320",),
                confidence=1.0,
                resolved_at=datetime.now(UTC),
            )
        )
        # Ambiguous pair: both carry "RSV" as a synonym.
        for iri, label, extra in [
            ("http://purl.obolibrary.org/obo/NCBITaxon_11246", "Bovine orthopneumovirus", "BRSV"),
            ("http://purl.obolibrary.org/obo/NCBITaxon_11250", "Human orthopneumovirus", "HRSV"),
        ]:
            writer.write_entry(
                DictionaryEntry(
                    entity_type=EntityType.PATHOGEN,
                    canonical_iri=iri,
                    canonical_label=label,
                    synonyms=(extra, "RSV"),
                    ontology=OntologyName.NCBITAXON,
                    ontology_version="2026-04-01",
                    source_records=(f"ncbitaxon.species.{iri.rsplit('_', 1)[-1]}",),
                    confidence=1.0,
                    resolved_at=datetime.now(UTC),
                )
            )
        writer.write_manifest(manifest)


def test_cli_unambiguous_hit_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cli.sqlite"
    _write_test_dictionary(db)
    rc = main(["ZIKV", "--type", "pathogen", "--dict-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "path         : fast" in out
    assert "NCBITaxon_64320" in out
    assert "Zika virus" in out


def test_cli_miss_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cli.sqlite"
    _write_test_dictionary(db)
    rc = main(["definitely_not_a_real_thing_xyz", "--dict-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "path         : miss" in out


def test_cli_ambiguous_surfaces_candidate_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SC-A5b contract: ambiguous surface forms print ALL candidates, exit 0."""
    db = tmp_path / "cli.sqlite"
    _write_test_dictionary(db)
    rc = main(["RSV", "--type", "pathogen", "--dict-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "path         : ambiguous" in out
    assert "candidates   : 2" in out
    assert "NCBITaxon_11246" in out
    assert "NCBITaxon_11250" in out
    assert "HITL required" in out


def test_cli_json_output_is_valid_json_with_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.sqlite"
    _write_test_dictionary(db)
    rc = main(["RSV", "--type", "pathogen", "--json", "--dict-path", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["path"] == "ambiguous"
    assert payload["resolution_status"] == "ambiguous"
    assert payload["canonical_iri"] is None  # SC-A5b contract
    assert len(payload["candidates"]) == 2


def test_cli_missing_dictionary_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["ZIKV", "--dict-path", str(tmp_path / "nope.sqlite")])
    err = capsys.readouterr().err
    assert rc == 3
    assert "dictionary artifact not found" in err
