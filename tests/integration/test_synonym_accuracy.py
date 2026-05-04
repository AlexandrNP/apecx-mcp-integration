"""Synonym-dictionary accuracy metrics — pathogens against VIOLIN ground truth.

These tests build a real dictionary from a VIOLIN slice, then measure how
accurately the dictionary recovers the row's known NCBI_Taxonomy_ID from
the row's free-form ``Pathogen`` name. The harness is in
``src/apecx_integration/synonym_dictionary/metrics.py``.

Two modes
=========
- **Baseline assertion** (``test_pathogen_baseline_metrics``):
  asserts a minimum recall + F1 floor so a regression in dictionary build
  or lookup logic flips the test red. The floor is set conservatively
  based on observed numbers so a small data drift doesn't false-fire.
- **Diagnostic dump** (``test_pathogen_metrics_print_summary``):
  doesn't assert anything strict — it prints the summary + sample
  mismatches to pytest's captured stdout so a developer running
  ``pytest -s`` sees concrete improvement targets for the synonym
  expansion task.

Gates
-----
``APECX_SYNONYM_DICT_LIVE_OLS=1`` because building requires live OLS.
``APECX_DATA_ROOT`` (or workspace ``data/``) for VIOLIN CSV.

Why this is a separate file from test_p39_*
-------------------------------------------
test_p39_* covers the PRECISION-FILTER MECHANISM (the _resolution
key wiring through MCP tools). This file covers the RESOLVER ACCURACY
(do the right IRIs come out at all). Different concerns; same dataset.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_PATHOGENS = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE_OLS,
    reason="Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run live-OLS accuracy tests.",
)


# We measure on a 60-row slice. Bigger slices catch more diversity but
# OLS rate-limits and the tests get slow. 60 rows covers EEEV (row 50)
# plus a representative spread of common pathogens.
SLICE_SIZE = 60


@pytest.fixture(scope="module")
def baseline_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a real Stage 1 dictionary from the first SLICE_SIZE VIOLIN rows."""
    assert VIOLIN_PATHOGENS.exists(), f"VIOLIN data missing at {VIOLIN_PATHOGENS}"

    from apecx_integration.synonym_dictionary.cli import main as build_main

    out = tmp_path_factory.mktemp("baseline_accuracy_dict")
    ret = build_main(
        [
            "--violin-pathogens",
            str(VIOLIN_PATHOGENS),
            "--output",
            str(out),
            "--dictionary-version",
            "test-baseline-accuracy",
            "--max-rows",
            str(SLICE_SIZE),
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, f"apecx-build-dictionary exited with code {ret}"
    db = out / "dictionary.sqlite"
    assert db.exists()
    return db


def test_pathogen_baseline_metrics(baseline_dictionary: Path):
    """The current build + lookup configuration must hit a recall floor.

    Conservative floors so a small data drift doesn't false-fire.
    Tighten when the synonym-expansion work lands.
    """
    from apecx_integration.synonym_dictionary.metrics import measure_pathogen_accuracy

    metrics = measure_pathogen_accuracy(baseline_dictionary, VIOLIN_PATHOGENS, max_rows=SLICE_SIZE)

    print("\n=== BASELINE PATHOGEN ACCURACY ===")
    print(metrics.summary())

    assert metrics.total_rows == SLICE_SIZE
    assert (
        metrics.rows_with_ground_truth > 0
    ), "No rows had a non-null NCBI_Taxonomy_ID — VIOLIN CSV may be malformed."

    # Floors locked in 2026-05-04 after the database-specific-entry fix
    # in PathogenResolver._resolve_by_iri (deprecated NCBI taxon IDs now
    # fall back to VIOLIN's label as canonical). Pre-fix baseline was
    # recall 0.949; post-fix is 1.000. The floor sits below 1.0 to absorb
    # small drift if NCBI fixes a deprecation or VIOLIN updates a row.
    assert metrics.recall >= 0.95, (
        f"Recall regression: got {metrics.recall:.3f}, expected ≥0.95. "
        f"Summary:\n{metrics.summary()}"
    )
    assert metrics.precision >= 0.95, (
        f"Precision regression: got {metrics.precision:.3f}, expected ≥0.95. "
        f"Summary:\n{metrics.summary()}"
    )
    # F1 catches the case where one metric trades off the other.
    assert metrics.f1 >= 0.95, (
        f"F1 regression: got {metrics.f1:.3f}, expected ≥0.95. " f"Summary:\n{metrics.summary()}"
    )


def test_pathogen_metrics_print_summary(baseline_dictionary: Path):
    """Diagnostic dump — no strict asserts.

    Run with ``pytest -s`` to see the human-readable summary + first 10
    mismatches. Useful as a reference for the synonym-expansion task:
    each mismatch is a concrete failure mode to drive recall/precision up.
    """
    from apecx_integration.synonym_dictionary.metrics import measure_pathogen_accuracy

    metrics = measure_pathogen_accuracy(baseline_dictionary, VIOLIN_PATHOGENS, max_rows=SLICE_SIZE)

    print("\n" + "=" * 60)
    print("PATHOGEN ACCURACY DIAGNOSTIC DUMP")
    print("=" * 60)
    print(metrics.summary())

    if metrics.mismatches:
        print("\nFirst mismatches (up to 10):")
        for mm in metrics.mismatches[:10]:
            print(f"  - {mm}")
    if metrics.miss_with_truth_count > 0:
        print(
            f"\n{metrics.miss_with_truth_count} ground-truth rows produced 'miss' "
            "— candidate targets for synonym expansion."
        )
