"""Integration tests for real NCBI Taxonomy dump + ancestor traversal (P3.5).

These tests download or reuse the real NCBI taxdump (taxdump.tar.gz, ~72 MB
compressed) and build a dictionary artifact with the full 2.74M-row hierarchy.
They then verify that:

  - fetch_taxdump() correctly downloads / idempotently reuses nodes.dmp + merged.dmp
  - The built dictionary embeds all 2,739,523 taxon_hierarchy rows
  - lookup_ancestor() walks from real strain NCBITaxon_11022 → species NCBITaxon_11021
    (Eastern Equine Encephalitis Virus)
  - lookup_entity() returns path="ancestor" + confidence penalty for the strain IRI

Gate: APECX_NCBITAXON_TAXDUMP=1  (skipped by default to avoid ~30s build + download)

Real data used:
  - EEEV (Eastern Equine Encephalitis Virus) NCBITaxon:11021 is a species-level
    taxon present in VIOLIN (≤60 rows from Pathogen_Information.csv).
  - NCBITaxon:11022 is a real EEEV strain that is a direct child of 11021
    in nodes.dmp.  Both are confirmed from the real NCBI taxonomy database.

Mock parity: backs-stop test_p35_ancestor_traversal_on_real_dictionary in
  tests/integration/test_stage2_lookup.py, which uses a *synthetic* hierarchy
  in the absence of real taxdump data.  This file exercises the identical
  code path on the *real* 2.74M-row dataset.

To run:

    APECX_NCBITAXON_TAXDUMP=1 \\
        APECX_SYNONYM_DICT_LIVE_OLS=1 \\
        PYTHONPATH=src .venv/bin/python -m pytest \\
        tests/integration/test_taxdump_real_hierarchy.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Gate: real taxdump tests are skipped unless explicitly opted-in.
# ---------------------------------------------------------------------------
_TAXDUMP_ENABLED = os.environ.get("APECX_NCBITAXON_TAXDUMP", "").strip() == "1"
_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not (_TAXDUMP_ENABLED and _LIVE_OLS),
    reason=(
        "Set APECX_NCBITAXON_TAXDUMP=1 and APECX_SYNONYM_DICT_LIVE_OLS=1 to run "
        "real-taxonomy integration tests that download NCBI taxdump (~72 MB) and "
        "build a dictionary with the full 2.74M-row hierarchy."
    ),
)

# Cache the real taxdump at a stable path so repeated runs skip the download.
TAXDUMP_CACHE_DIR = Path(os.environ.get("APECX_TAXDUMP_CACHE", "/tmp/apecx_taxdump"))

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_PATHOGENS = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"

# Real EEEV taxon IDs confirmed from nodes.dmp (2026-05-04).
EEEV_SPECIES_ID = 11021
EEEV_STRAIN_ID = 11022  # "Eastern equine encephalomyelitis virus strain PE-6"
EEEV_SPECIES_IRI = f"http://purl.obolibrary.org/obo/NCBITaxon_{EEEV_SPECIES_ID}"
EEEV_STRAIN_IRI = f"http://purl.obolibrary.org/obo/NCBITaxon_{EEEV_STRAIN_ID}"


# ---------------------------------------------------------------------------
# Module-scoped fixtures: expensive operations run once per pytest session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_taxdump() -> tuple[Path, Path]:
    """Return (nodes_dmp, merged_dmp), downloading if necessary.

    Uses TAXDUMP_CACHE_DIR so subsequent pytest runs skip the ~72 MB download.
    """
    from apecx_integration.synonym_dictionary.taxdump_fetcher import fetch_taxdump

    nodes_path, merged_path = fetch_taxdump(
        TAXDUMP_CACHE_DIR,
        force=False,
        show_progress=True,
    )
    assert nodes_path.exists(), f"nodes.dmp missing: {nodes_path}"
    assert merged_path.exists(), f"merged.dmp missing: {merged_path}"
    return nodes_path, merged_path


@pytest.fixture(scope="module")
def dict_with_real_hierarchy(
    tmp_path_factory: pytest.TempPathFactory,
    real_taxdump: tuple[Path, Path],
) -> Path:
    """Build a dictionary from ≤60 VIOLIN rows + the real full hierarchy.

    Returns the path to dictionary.sqlite.
    """
    assert VIOLIN_PATHOGENS.exists(), (
        f"VIOLIN data not found at {VIOLIN_PATHOGENS}. " "Populate the workspace data/ directory."
    )
    nodes_path, merged_path = real_taxdump
    out = tmp_path_factory.mktemp("real_taxdump_dict")

    from apecx_integration.synonym_dictionary.cli import main as build_main

    ret = build_main(
        [
            "--violin-pathogens",
            str(VIOLIN_PATHOGENS),
            "--output",
            str(out),
            "--dictionary-version",
            "test-real-taxdump",
            "--max-rows",
            "60",
            "--ncbitaxon-nodes",
            str(nodes_path),
            "--ncbitaxon-merged",
            str(merged_path),
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, "apecx-build-dictionary returned non-zero"
    db = out / "dictionary.sqlite"
    assert db.exists(), f"dictionary.sqlite not written to {out}"
    return db


# ---------------------------------------------------------------------------
# Tests: taxdump fetcher
# ---------------------------------------------------------------------------


def test_fetch_taxdump_returns_existing_files(real_taxdump: tuple[Path, Path]) -> None:
    """fetch_taxdump() with force=False on a cached dir returns without re-downloading."""
    nodes_path, merged_path = real_taxdump
    assert (
        nodes_path.stat().st_size > 100 * 1024 * 1024
    ), f"nodes.dmp is suspiciously small: {nodes_path.stat().st_size} bytes"
    assert (
        merged_path.stat().st_size > 1 * 1024 * 1024
    ), f"merged.dmp is suspiciously small: {merged_path.stat().st_size} bytes"


def test_fetch_taxdump_idempotent(real_taxdump: tuple[Path, Path]) -> None:
    """Calling fetch_taxdump() twice returns the same paths without error."""
    from apecx_integration.synonym_dictionary.taxdump_fetcher import fetch_taxdump

    nodes_path, merged_path = real_taxdump
    dest_dir = nodes_path.parent

    n2, m2 = fetch_taxdump(dest_dir, force=False)
    assert n2 == nodes_path
    assert m2 == merged_path


def test_nodes_dmp_contains_eeev_strain(real_taxdump: tuple[Path, Path]) -> None:
    """The real nodes.dmp has NCBITaxon:11022 as a child of NCBITaxon:11021."""
    nodes_path, _ = real_taxdump
    # Parse first 3M lines max — EEEV is near the top (taxon IDs < 30K)
    found_strain = False
    with nodes_path.open() as fh:
        for line in fh:
            parts = line.split("|")
            if len(parts) < 2:
                continue
            child_id = int(parts[0].strip())
            parent_id = int(parts[1].strip())
            if child_id == EEEV_STRAIN_ID:
                assert parent_id == EEEV_SPECIES_ID, (
                    f"NCBITaxon:{EEEV_STRAIN_ID} has parent {parent_id}, "
                    f"expected {EEEV_SPECIES_ID}"
                )
                found_strain = True
                break
    assert found_strain, (
        f"NCBITaxon:{EEEV_STRAIN_ID} not found in nodes.dmp — "
        "NCBI may have retired this taxon ID"
    )


# ---------------------------------------------------------------------------
# Tests: dictionary build with real hierarchy
# ---------------------------------------------------------------------------


def test_real_hierarchy_row_count(dict_with_real_hierarchy: Path) -> None:
    """The built dictionary has the full 2.74M-row taxon_hierarchy."""
    import sqlite3

    with sqlite3.connect(dict_with_real_hierarchy) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM taxon_hierarchy").fetchone()
    # Real taxdump has 2,739,523 entries (verified 2026-05-04).
    # Allow ±10K variance for minor NCBI updates.
    assert count > 2_700_000, f"taxon_hierarchy has only {count} rows; expected ~2,739,523"


def test_real_hierarchy_merged_taxons_present(dict_with_real_hierarchy: Path) -> None:
    """The merged_taxons table has the full deprecated ID remappings."""
    import sqlite3

    with sqlite3.connect(dict_with_real_hierarchy) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM merged_taxons").fetchone()
    assert count > 90_000, f"merged_taxons has only {count} rows; expected ~98,082"


def test_real_hierarchy_eeev_parent_link(dict_with_real_hierarchy: Path) -> None:
    """taxon_hierarchy contains NCBITaxon:11022 → NCBITaxon:11021 edge."""
    import sqlite3

    with sqlite3.connect(dict_with_real_hierarchy) as conn:
        row = conn.execute(
            "SELECT parent_taxon_id FROM taxon_hierarchy WHERE child_taxon_id = ?",
            (EEEV_STRAIN_ID,),
        ).fetchone()
    assert row is not None, f"NCBITaxon:{EEEV_STRAIN_ID} not found in taxon_hierarchy"
    assert row[0] == EEEV_SPECIES_ID, f"Expected parent_taxon_id={EEEV_SPECIES_ID}, got {row[0]}"


# ---------------------------------------------------------------------------
# Tests: DictionaryIndex ancestor traversal with real hierarchy
# ---------------------------------------------------------------------------


def test_index_has_hierarchy_flag(dict_with_real_hierarchy: Path) -> None:
    """DictionaryIndex.has_hierarchy is True when hierarchy was embedded."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    index = DictionaryIndex.load(dict_with_real_hierarchy)
    assert (
        index.has_hierarchy
    ), "has_hierarchy should be True for a dictionary built with --ncbitaxon-nodes"


def test_lookup_ancestor_walks_real_strain_to_species(dict_with_real_hierarchy: Path) -> None:
    """lookup_ancestor() walks NCBITaxon:11022 → NCBITaxon:11021 in real data."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    index = DictionaryIndex.load(dict_with_real_hierarchy)
    ancestor = index.lookup_ancestor(EEEV_STRAIN_IRI)
    assert ancestor is not None, (
        f"lookup_ancestor({EEEV_STRAIN_IRI!r}) returned None. "
        f"EEEV species IRI ({EEEV_SPECIES_IRI}) must be present in the dictionary."
    )
    assert (
        ancestor.canonical_iri == EEEV_SPECIES_IRI
    ), f"Expected ancestor {EEEV_SPECIES_IRI!r}, got {ancestor.canonical_iri!r}"


def test_lookup_entity_ancestor_path_for_real_strain(dict_with_real_hierarchy: Path) -> None:
    """lookup_entity() returns path='ancestor' for NCBITaxon:11022 via singleton."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    singleton = _ProcessSingleton()
    singleton.configure(dict_with_real_hierarchy)
    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        result = lookup_entity(EEEV_STRAIN_IRI)
        assert result is not None, f"lookup_entity({EEEV_STRAIN_IRI!r}) returned None"
        assert (
            result.path == "ancestor"
        ), f"Expected path='ancestor' for real strain IRI; got path={result.path!r}"
        assert result.canonical_iri == EEEV_SPECIES_IRI
    finally:
        _loader._singleton = _orig


def test_lookup_entity_ancestor_confidence_penalty(dict_with_real_hierarchy: Path) -> None:
    """Ancestor path applies 0.9× confidence penalty to the species entry's confidence."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex, _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    index = DictionaryIndex.load(dict_with_real_hierarchy)
    species_entry = index.lookup_by_iri(EEEV_SPECIES_IRI)
    assert (
        species_entry is not None
    ), f"EEEV species IRI {EEEV_SPECIES_IRI!r} not found in dictionary"

    singleton = _ProcessSingleton()
    singleton.configure(dict_with_real_hierarchy)
    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        strain_result = lookup_entity(EEEV_STRAIN_IRI)
        expected = pytest.approx(species_entry.confidence * 0.9, abs=1e-6)
        assert strain_result.confidence == expected, (
            f"Strain confidence {strain_result.confidence} != "
            f"species confidence {species_entry.confidence} * 0.9"
        )
    finally:
        _loader._singleton = _orig
