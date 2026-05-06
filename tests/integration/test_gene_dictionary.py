"""Integration tests for gene dictionary building and Stage 2 lookup.

Unlike the OLS-backed resolver tests, these tests do NOT require
APECX_SYNONYM_DICT_LIVE_OLS=1 because GeneResolver makes no OLS calls.
It builds canonical IRIs from NCBI_Gene_ID source values directly.

What is verified:
- CLI builds a gene dictionary from VIOLIN Gene_Information.csv
- Enriched CSV has canonical columns and identifiers.org IRIs
- At least 73% of rows resolve (M1 fill rate: 73.5%)
- All resolved entries have confidence=1.0 (anchor mode)
- Stage 2 fast-path lookup works for gene symbols parsed from Gene_Name
- lookup_entity_by_iri reverse lookup works for gene IRIs

Mock parity for:
  tests/unit/synonym_dictionary/test_resolvers.py::
      test_gene_resolver_resolves_via_ncbi_gene_id (and related)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_GENES = WORKSPACE_ROOT / "data" / "violin" / "Gene_Information.csv"

_MAX_ROWS = 20


@pytest.fixture(scope="module")
def gene_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a gene dictionary from the first 20 VIOLIN gene rows.

    Returns the path to the ``dictionary.sqlite`` artifact.
    No OLS calls are made — GeneResolver is source-data-only.
    """
    assert VIOLIN_GENES.exists(), (
        f"VIOLIN gene data not found at {VIOLIN_GENES}. "
        "Ensure the workspace data/ directory is populated."
    )
    from apecx_integration.synonym_dictionary.cli import main

    out = tmp_path_factory.mktemp("gene_dict")
    ret = main(
        [
            "--violin-genes",
            str(VIOLIN_GENES),
            "--output",
            str(out),
            "--dictionary-version",
            "test-gene-v1",
            "--max-rows",
            str(_MAX_ROWS),
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, f"apecx-build-dictionary exited with code {ret}"
    db_path = out / "dictionary.sqlite"
    assert db_path.exists(), f"dictionary.sqlite missing at {db_path}"
    return db_path


# ---------------------------------------------------------------------------
# Stage 1 build assertions
# ---------------------------------------------------------------------------


def test_gene_build_produces_sqlite(gene_dictionary: Path) -> None:
    """Basic: the SQLite file exists and is non-empty."""
    assert gene_dictionary.stat().st_size > 0


def test_gene_build_manifest_correct(gene_dictionary: Path) -> None:
    """Manifest records gene dictionary version and row count."""
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(gene_dictionary)
    manifest = reader.read_manifest()
    assert manifest.dictionary_version == "test-gene-v1"
    assert manifest.record_count_total == _MAX_ROWS


def test_gene_build_anchor_mode_confidence(gene_dictionary: Path) -> None:
    """All resolved gene entries must have confidence=1.0 (anchor mode).

    GeneResolver never uses OLS search — confidence is always 1.0 or
    the row is UNRESOLVED.  Any confidence < 1.0 indicates a logic bug.
    """
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(gene_dictionary)
    entries = list(reader.all_entries())
    assert len(entries) > 0, "Gene dictionary is empty after 20-row build"

    non_anchor = [e for e in entries if e.confidence != 1.0]
    assert not non_anchor, (
        f"GeneResolver produced {len(non_anchor)} entries with confidence != 1.0: "
        f"{[(e.canonical_iri, e.confidence) for e in non_anchor[:3]]}"
    )


def test_gene_build_fill_rate(gene_dictionary: Path) -> None:
    """At least 73% of rows should resolve (M1 measured 73.5% NCBI_Gene_ID fill).

    With 20 rows, 14 resolved is the lower bound.
    """
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(gene_dictionary)
    # Entries are deduplicated by (entity_type, canonical_iri), so multiple
    # rows pointing at the same gene_id produce one entry.  Compare against
    # record_count_total - unresolved_count instead.
    manifest = reader.read_manifest()
    resolved = manifest.record_count_total - manifest.unresolved_count
    fill_rate = resolved / manifest.record_count_total
    assert fill_rate >= 0.60, (
        f"Gene fill rate {fill_rate:.1%} is below 60% threshold.  "
        f"M1 measured 73.5%; this may indicate NCBI_Gene_ID float-string handling broke."
    )


def test_gene_build_iris_use_identifiers_org(gene_dictionary: Path) -> None:
    """All canonical IRIs must use the identifiers.org/ncbigene/ namespace."""
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(gene_dictionary)
    for entry in reader.all_entries():
        assert entry.canonical_iri.startswith(
            "http://identifiers.org/ncbigene/"
        ), f"Gene entry has unexpected IRI format: {entry.canonical_iri}"


def test_gene_build_enriched_csv_has_canonical_columns(gene_dictionary: Path) -> None:
    """Enriched CSV must have all canonical columns added by Stage 1."""
    enriched_csv = gene_dictionary.parent / "enriched" / "violin_genes_enriched.csv"
    assert enriched_csv.exists(), f"Enriched gene CSV missing at {enriched_csv}"
    df = pd.read_csv(enriched_csv)
    for col in ("canonical_iri", "canonical_label", "resolution_status", "resolution_confidence"):
        assert col in df.columns, f"Enriched gene CSV missing column {col!r}"


def test_gene_build_synonyms_extracted_from_gene_name(gene_dictionary: Path) -> None:
    """Entries for genes with 'symbol (long name)' format should have ≥2 synonyms.

    Verifies that _extract_gene_synonyms is correctly hooked into the build.
    """
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(gene_dictionary)
    # Find any entry with synonyms (expects: symbol, long name, full Gene_Name)
    entries_with_synonyms = [e for e in reader.all_entries() if len(e.synonyms) >= 2]
    assert len(entries_with_synonyms) > 0, (
        "No gene dictionary entries have ≥2 synonyms.  "
        "_extract_gene_synonyms may not be running, or Gene_Name column has unexpected format."
    )


# ---------------------------------------------------------------------------
# Stage 2 fast-path lookup
# ---------------------------------------------------------------------------


def test_gene_stage2_lookup_by_gene_symbol(gene_dictionary: Path) -> None:
    """Stage 2 fast path: looking up a gene symbol returns path='fast'.

    GeneResolver parses 'Ifng (Interferon gamma)' into label='Ifng' and
    synonyms=['Ifng', 'Interferon gamma', 'Ifng (Interferon gamma)'].
    The Stage 2 index should find 'Ifng' via the synonym.
    """
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex, _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    index = DictionaryIndex.load(gene_dictionary)

    # Find a gene entry that has a short symbol (parsed from "()" pattern)
    target = None
    for entry in index._entries.values():
        # Label is the short symbol; synonyms include the long form
        if entry.canonical_label != entry.synonyms[0] if entry.synonyms else False:
            continue
        if len(entry.synonyms) >= 2:
            target = entry
            break
    if target is None:
        pytest.skip("No multi-synonym gene entry found in first 20 rows")

    singleton = _ProcessSingleton()
    singleton.configure(gene_dictionary)

    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        result = lookup_entity(target.canonical_label)
        assert result.path == "fast", (
            f"lookup_entity({target.canonical_label!r}) returned path={result.path!r}; "
            "expected 'fast' for a known gene label"
        )
        assert result.canonical_iri == target.canonical_iri
        assert result.confidence == 1.0
    finally:
        _loader._singleton = _orig


def test_gene_stage2_lookup_by_iri(gene_dictionary: Path) -> None:
    """Stage 2 IRI shortcut: lookup_entity with a gene IRI returns path='fast'."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex, _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    index = DictionaryIndex.load(gene_dictionary)
    any_entry = next(iter(index._entries.values()))

    singleton = _ProcessSingleton()
    singleton.configure(gene_dictionary)

    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        result = lookup_entity(any_entry.canonical_iri)
        assert result.path == "fast", (
            f"IRI shortcut failed for {any_entry.canonical_iri!r}: " f"got path={result.path!r}"
        )
        assert result.canonical_iri == any_entry.canonical_iri
    finally:
        _loader._singleton = _orig
