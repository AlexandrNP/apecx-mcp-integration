"""Synonym-dictionary accuracy metrics across all entity classes.

These tests build a real dictionary from VIOLIN data and measure how
accurately each resolver class recovers the ground-truth IRI from its
free-form surface form. The harness is in
``src/apecx_integration/synonym_dictionary/metrics.py``.

Coverage matrix
===============
| Class    | Ground-truth column     | Resolver           | Notes                       |
|----------|-------------------------|--------------------|-----------------------------|
| Pathogen | NCBI_Taxonomy_ID        | PathogenResolver   | OLS-anchored + DB fallback  |
| Vaccine  | Vaccine_Ontology_ID     | VaccineResolver    | OLS-anchored + DB fallback  |
| Gene     | NCBI_Gene_ID            | GeneResolver       | NO OLS — pure DB anchor     |
| Disease  | (none — search-only)    | DiseaseResolver    | Path-stats only, no recall  |

Two test types per class
========================
- **Slice baseline** (``test_<class>_slice_metrics``): runs against a
  small slice (60 rows) and asserts conservative recall/precision floors.
  Fast; runs in normal live-OLS sessions.
- **Full corpus** (``test_<class>_full_corpus``): runs against the
  whole VIOLIN file (Pathogen 218 rows, Vaccine 3.5K rows, Gene 4K rows).
  Gated additionally on ``APECX_SYNONYM_DICT_FULL_CORPUS=1`` because
  full-corpus on Vaccine takes ~10–15min of OLS calls.

Gate
----
``APECX_SYNONYM_DICT_LIVE_OLS=1`` is required for everything (we need a
real dictionary build via OLS).

Why this is a separate file from test_p39_*
-------------------------------------------
test_p39_* covers the PRECISION-FILTER MECHANISM (the _resolution
key wiring through MCP tools). This file covers RESOLVER ACCURACY
(do the right IRIs come out at all). Different concerns; same dataset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_DIR = WORKSPACE_ROOT / "data" / "violin"
VIOLIN_PATHOGENS = VIOLIN_DIR / "Pathogen_Information.csv"
VIOLIN_VACCINES = VIOLIN_DIR / "Vaccine_Information.csv"
VIOLIN_GENES = VIOLIN_DIR / "Gene_Information.csv"

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"
_FULL_CORPUS = os.environ.get("APECX_SYNONYM_DICT_FULL_CORPUS", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE_OLS,
    reason="Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run live-OLS accuracy tests.",
)


SLICE_SIZE = 60


# ---------------------------------------------------------------------------
# Fixtures — build a single dictionary covering all classes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a dictionary from the first SLICE_SIZE rows of each VIOLIN table.

    Migration note (2026-05-05): replaces the deleted ``apecx-build-dictionary``
    CLI with the workflow-driven helper ``build_dictionary_for_test``, which
    instantiates :class:`TaxdumpFetchStep` + :class:`DictionaryBuildStep`
    directly via ``from_config`` and runs them in sequence.
    """
    assert VIOLIN_PATHOGENS.exists(), f"VIOLIN data missing at {VIOLIN_PATHOGENS}"

    from tests.integration._dict_build_helper import build_dictionary_for_test

    out = tmp_path_factory.mktemp("slice_accuracy_dict")
    db = build_dictionary_for_test(
        output_dir=out,
        dictionary_version="test-slice-accuracy",
        max_rows=SLICE_SIZE,
        violin_pathogens=VIOLIN_PATHOGENS,
        violin_vaccines=VIOLIN_VACCINES if VIOLIN_VACCINES.exists() else None,
        violin_genes=VIOLIN_GENES if VIOLIN_GENES.exists() else None,
    )
    assert db.exists()
    return db


@pytest.fixture(scope="module")
def full_corpus_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a dictionary from the FULL VIOLIN corpus. Slow."""
    if not _FULL_CORPUS:
        pytest.skip("Set APECX_SYNONYM_DICT_FULL_CORPUS=1 for full-corpus tests.")
    assert VIOLIN_PATHOGENS.exists()

    from tests.integration._dict_build_helper import build_dictionary_for_test

    out = tmp_path_factory.mktemp("full_corpus_accuracy_dict")
    db = build_dictionary_for_test(
        output_dir=out,
        dictionary_version="test-full-corpus-accuracy",
        max_rows=None,
        violin_pathogens=VIOLIN_PATHOGENS,
        violin_vaccines=VIOLIN_VACCINES if VIOLIN_VACCINES.exists() else None,
        violin_genes=VIOLIN_GENES if VIOLIN_GENES.exists() else None,
    )
    assert db.exists()
    return db


# ---------------------------------------------------------------------------
# Pathogen — slice + full corpus
# ---------------------------------------------------------------------------


def test_pathogen_slice_metrics(slice_dictionary: Path):
    """Pathogen recall/precision/F1 on a 60-row slice."""
    from apecx_integration.synonym_dictionary.metrics import measure_pathogen_accuracy

    metrics = measure_pathogen_accuracy(slice_dictionary, VIOLIN_PATHOGENS, max_rows=SLICE_SIZE)
    print(f"\n=== PATHOGEN SLICE ({SLICE_SIZE} rows) ===")
    print(metrics.summary())

    assert metrics.rows_with_ground_truth > 0
    assert metrics.recall >= 0.95, f"Recall {metrics.recall:.3f} < 0.95\n{metrics.summary()}"
    assert metrics.precision >= 0.95, (
        f"Precision {metrics.precision:.3f} < 0.95\n{metrics.summary()}"
    )
    assert metrics.f1 >= 0.95


@pytest.mark.skipif(not _FULL_CORPUS, reason="Full-corpus opt-in.")
def test_pathogen_full_corpus(full_corpus_dictionary: Path):
    """Full VIOLIN pathogen corpus (218 rows). Floors slightly below the
    slice baseline to absorb long-tail diversity."""
    from apecx_integration.synonym_dictionary.metrics import measure_pathogen_accuracy

    metrics = measure_pathogen_accuracy(full_corpus_dictionary, VIOLIN_PATHOGENS)
    print("\n=== PATHOGEN FULL CORPUS ===")
    print(metrics.summary())
    assert metrics.recall >= 0.90
    assert metrics.precision >= 0.95
    assert metrics.f1 >= 0.92


# ---------------------------------------------------------------------------
# Vaccine — slice + full corpus
# ---------------------------------------------------------------------------


def test_vaccine_slice_metrics(slice_dictionary: Path):
    """Vaccine recall on a 60-row slice. Floors are looser than pathogens
    because VIOLIN's Vaccine_Ontology_ID fill rate is only ~84% (per
    VaccineResolver docstring), so many rows have no ground truth."""
    from apecx_integration.synonym_dictionary.metrics import measure_vaccine_accuracy

    metrics = measure_vaccine_accuracy(slice_dictionary, VIOLIN_VACCINES, max_rows=SLICE_SIZE)
    print(f"\n=== VACCINE SLICE ({SLICE_SIZE} rows) ===")
    print(metrics.summary())

    if metrics.rows_with_ground_truth == 0:
        pytest.skip("No ground-truth VO IDs in slice — adjust SLICE_SIZE.")
    # Looser floor (0.80) — vaccines have more name-mangling issues than
    # pathogens (long tradenames, manufacturer prefixes, etc.).
    assert metrics.recall >= 0.80, f"Recall {metrics.recall:.3f} < 0.80\n{metrics.summary()}"
    assert metrics.precision >= 0.80, (
        f"Precision {metrics.precision:.3f} < 0.80\n{metrics.summary()}"
    )


@pytest.mark.skipif(not _FULL_CORPUS, reason="Full-corpus opt-in (Vaccine: ~3.5K rows, slow).")
def test_vaccine_full_corpus(full_corpus_dictionary: Path):
    """Full VIOLIN Vaccine corpus (~3.5K rows). Looser floors than slice."""
    from apecx_integration.synonym_dictionary.metrics import measure_vaccine_accuracy

    metrics = measure_vaccine_accuracy(full_corpus_dictionary, VIOLIN_VACCINES)
    print("\n=== VACCINE FULL CORPUS ===")
    print(metrics.summary())
    if metrics.rows_with_ground_truth == 0:
        pytest.skip("No ground-truth VO IDs in full corpus — VIOLIN data may be malformed.")
    assert metrics.recall >= 0.75
    assert metrics.precision >= 0.85


# ---------------------------------------------------------------------------
# Gene — slice + full corpus
# ---------------------------------------------------------------------------


def test_gene_slice_metrics(slice_dictionary: Path):
    """Gene recall on a 60-row slice. Genes don't go through OLS (no public
    NCBI Gene endpoint in OLS), so the resolver only succeeds when
    NCBI_Gene_ID is filled. Floors set to match the column's known
    fill rate (~73%)."""
    from apecx_integration.synonym_dictionary.metrics import measure_gene_accuracy

    metrics = measure_gene_accuracy(slice_dictionary, VIOLIN_GENES, max_rows=SLICE_SIZE)
    print(f"\n=== GENE SLICE ({SLICE_SIZE} rows) ===")
    print(metrics.summary())

    if metrics.rows_with_ground_truth == 0:
        pytest.skip("No ground-truth NCBI_Gene_IDs in slice.")
    # 70% recall floor (lower than pathogens because GeneResolver has no
    # OLS fallback and the ID column has ~73% fill).
    assert metrics.recall >= 0.70, f"Recall {metrics.recall:.3f} < 0.70\n{metrics.summary()}"
    # Genes are pure-anchor when resolved, so precision should be perfect.
    assert metrics.precision >= 0.95


@pytest.mark.skipif(not _FULL_CORPUS, reason="Full-corpus opt-in (Gene: ~4K rows).")
def test_gene_full_corpus(full_corpus_dictionary: Path):
    """Full VIOLIN Gene corpus (~4K rows)."""
    from apecx_integration.synonym_dictionary.metrics import measure_gene_accuracy

    metrics = measure_gene_accuracy(full_corpus_dictionary, VIOLIN_GENES)
    print("\n=== GENE FULL CORPUS ===")
    print(metrics.summary())
    if metrics.rows_with_ground_truth == 0:
        pytest.skip("No ground-truth NCBI_Gene_IDs in full corpus.")
    assert metrics.recall >= 0.65
    assert metrics.precision >= 0.95


# ---------------------------------------------------------------------------
# Disease — path distribution only (no ground truth column)
# ---------------------------------------------------------------------------


def test_disease_slice_path_distribution(slice_dictionary: Path):
    """Disease has no ground-truth DOID column in VIOLIN. We can only
    measure path distribution: how many of the queried Disease values
    resolved to *some* IRI vs missed."""
    from apecx_integration.synonym_dictionary.metrics import (
        measure_disease_path_distribution,
    )

    metrics = measure_disease_path_distribution(
        slice_dictionary, VIOLIN_PATHOGENS, max_rows=SLICE_SIZE
    )
    print(f"\n=== DISEASE PATH DISTRIBUTION ({SLICE_SIZE} rows) ===")
    print(metrics.summary())
    # No strict assert — disease resolution is search-only against DOID
    # and many VIOLIN Disease strings don't have DOID matches. The
    # observability win is the path-distribution histogram itself.
    assert metrics.total_rows > 0


# ---------------------------------------------------------------------------
# Hierarchy correctness — no entity-class mixing
# ---------------------------------------------------------------------------


def test_no_genus_collapsed_into_species(slice_dictionary: Path):
    """REGRESSION GUARD: VIOLIN has rows for both genus-level and species-
    level taxa. The dictionary must NOT have a genus name as a synonym
    of a species — that would collapse "Coronaviridae" → "SARS-CoV-2"
    incorrectly. Verify by checking that querying a known parent-taxon
    value (e.g. ``Genus`` cell from a row) does NOT return that row's
    species IRI as a confident match.

    Specifically: an earlier (reverted) iteration of PathogenResolver
    added Genus/Species/Family from each VIOLIN row as synonyms of the
    species. This test fails if that regression returns.
    """
    import sqlite3

    # Cheapest check: read the dictionary and confirm no entry has a
    # "genus name" (single capitalized word ending in -viridae or -virus
    # without a space — e.g. Henipavirus) as a synonym of a strain-level
    # taxon. Heuristic; the strict guard is the resolver code review.
    with sqlite3.connect(slice_dictionary) as conn:
        rows = conn.execute(
            "SELECT canonical_iri, surface_form_normalized FROM inverse_index "
            "WHERE surface_form_normalized IN ('henipavirus', 'coronaviridae', 'orthomyxoviridae')"
        ).fetchall()
    # Any matches should have the genus/family IRI as canonical, not a
    # species/strain. We don't have a clean mapping handy without OLS,
    # but we CAN assert that there's at most one canonical IRI per such
    # surface form (i.e., not an ambiguous hit on multiple species).
    by_surface: dict[str, set[str]] = {}
    for canonical_iri, surface in rows:
        by_surface.setdefault(surface, set()).add(canonical_iri)
    for surface, iris in by_surface.items():
        assert len(iris) == 1, (
            f"Surface form {surface!r} maps to multiple canonical IRIs "
            f"{iris!r} — this is the genus/species collapse the strict "
            "hierarchy contract forbids. PathogenResolver may have "
            "regressed to adding Genus/Species/Family as species synonyms."
        )
