"""Accuracy metrics for synonym-dictionary lookups against ground-truth datasets.

What this module measures
=========================
Given a built synonym-dictionary SQLite artifact and a CSV of records with
known ground-truth IRIs, this module computes:

- **Recall**: of records whose ground-truth IRI is non-null, what fraction
  did the dictionary return *any* IRI for? (Any path: fast / ancestor / slow.)
- **Precision**: of records where the dictionary returned an IRI, what
  fraction matched the ground-truth IRI?
- **Path distribution**: how many records each lookup path produced.
- **Confidence histogram**: distribution of resolution_confidence values
  across the resolved rows.
- **Mismatches**: list of (surface_form, expected_iri, got_iri) for diagnostic
  inspection — which entries in the source CSV the dictionary disagrees with.
  Most useful as input to the synonym-expansion task.

Why this lives here, not in tests/
==================================
The harness is reusable: it backs the integration tests that capture a
baseline + the synonym-expansion work that expects to drive recall/precision
upward. Tests import ``measure_pathogen_accuracy``; ad-hoc CLI scripts
can also call it.

Design constraints
------------------
- Pure pandas + dictionary I/O — no MCP, no Control Plane.
- Configurable via the same ``DictionaryIndex`` + ``lookup_entity`` API
  the rest of the system uses; if those produce wrong answers, this
  harness reports them faithfully.
- ``max_rows`` parameter limits the input slice for fast iteration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.loader import (
    _ProcessSingleton,
    configure_dictionary_path,
)
from apecx_integration.synonym_dictionary.lookup import lookup_entity

log = logging.getLogger(__name__)

ResolutionPath = Literal["fast", "ancestor", "slow", "miss"]


@dataclass(frozen=True)
class Mismatch:
    """A single record where the dictionary disagrees with ground truth."""

    surface_form: str
    expected_iri: str
    got_iri: str | None
    got_path: ResolutionPath
    got_confidence: float
    got_label: str | None = None

    def __str__(self) -> str:
        return (
            f"{self.surface_form!r}: expected {self.expected_iri!r}, got "
            f"{self.got_iri!r} (path={self.got_path}, conf={self.got_confidence:.2f})"
        )


@dataclass
class AccuracyMetrics:
    """Aggregated accuracy report for a dictionary against a ground-truth dataset."""

    total_rows: int
    rows_with_ground_truth: int

    fast_count: int = 0
    ancestor_count: int = 0
    slow_count: int = 0
    miss_count: int = 0

    correct_count: int = 0
    incorrect_count: int = 0
    miss_with_truth_count: int = 0

    mismatches: list[Mismatch] = field(default_factory=list)
    confidence_buckets: dict[str, int] = field(default_factory=dict)

    @property
    def resolved_count(self) -> int:
        """Records the dictionary returned any IRI for (fast/ancestor/slow)."""
        return self.fast_count + self.ancestor_count + self.slow_count

    @property
    def recall(self) -> float:
        """Fraction of ground-truth rows where lookup returned any IRI.

        Recall = (resolved & has-ground-truth) / has-ground-truth.
        """
        resolved_with_truth = self.correct_count + self.incorrect_count
        return resolved_with_truth / max(self.rows_with_ground_truth, 1)

    @property
    def precision(self) -> float:
        """Fraction of resolved rows where IRI matches ground truth.

        Precision = correct / (correct + incorrect).
        """
        return self.correct_count / max(self.correct_count + self.incorrect_count, 1)

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall (single-score summary)."""
        p, r = self.precision, self.recall
        return 2 * p * r / max(p + r, 1e-9)

    def summary(self) -> str:
        """Human-readable report. Use in CLI / test logs."""
        lines = [
            f"Total rows examined:      {self.total_rows}",
            f"Rows with ground truth:   {self.rows_with_ground_truth}",
            f"  → resolved correctly:   {self.correct_count}",
            f"  → resolved incorrectly: {self.incorrect_count}",
            f"  → missed:               {self.miss_with_truth_count}",
            "",
            "Path distribution:",
            f"  fast:     {self.fast_count}",
            f"  ancestor: {self.ancestor_count}",
            f"  slow:     {self.slow_count}",
            f"  miss:     {self.miss_count}",
            "",
            f"Recall:    {self.recall:.3f}  ({self.correct_count + self.incorrect_count}/{self.rows_with_ground_truth})",
            f"Precision: {self.precision:.3f}  ({self.correct_count}/{self.correct_count + self.incorrect_count})",
            f"F1:        {self.f1:.3f}",
        ]
        if self.confidence_buckets:
            lines.append("")
            lines.append("Confidence buckets:")
            for bucket, count in sorted(self.confidence_buckets.items()):
                lines.append(f"  {bucket}: {count}")
        return "\n".join(lines)


def _build_expected_pathogen_iri(taxon_id: float | int | str | None) -> str | None:
    """Convert a VIOLIN ``NCBI_Taxonomy_ID`` cell to an OBO IRI, or None."""
    if taxon_id is None or pd.isna(taxon_id):
        return None
    try:
        as_int = int(float(taxon_id))
    except (TypeError, ValueError):
        return None
    return f"http://purl.obolibrary.org/obo/NCBITaxon_{as_int}"


def _confidence_bucket(confidence: float) -> str:
    """Human-readable confidence band."""
    if confidence >= 0.95:
        return "[0.95, 1.00]"
    if confidence >= 0.80:
        return "[0.80, 0.95)"
    if confidence >= 0.50:
        return "[0.50, 0.80)"
    if confidence > 0.0:
        return "(0.00, 0.50)"
    return "0.00"


def measure_pathogen_accuracy(
    dictionary_path: Path | str,
    violin_pathogens_csv: Path | str,
    *,
    max_rows: int | None = None,
    surface_form_column: str = "Pathogen",
    ground_truth_column: str = "NCBI_Taxonomy_ID",
) -> AccuracyMetrics:
    """Measure accuracy of pathogen IRI resolution against VIOLIN ground truth.

    Parameters
    ----------
    dictionary_path:
        Path to a built ``dictionary.sqlite`` artifact.
    violin_pathogens_csv:
        Path to ``Pathogen_Information.csv`` (or any CSV with columns matching
        ``surface_form_column`` + ``ground_truth_column``).
    max_rows:
        Cap the number of rows examined. ``None`` = examine all.
    surface_form_column:
        CSV column carrying the user-typed pathogen name. Default ``"Pathogen"``.
    ground_truth_column:
        CSV column carrying the integer NCBI taxon ID. Default
        ``"NCBI_Taxonomy_ID"``.

    Returns
    -------
    AccuracyMetrics. Side effect: configures the process-level
    dictionary singleton to point at ``dictionary_path``.

    Notes
    -----
    "Correct" means the returned ``canonical_iri`` exactly equals the
    expected IRI built from the ground-truth column. The dictionary may
    return an *ancestor* (species) when the ground truth is a strain; that
    counts as INCORRECT for precision purposes — the test is whether the
    dictionary recovers the operator's expected target, not whether it
    returns a related taxon.
    """
    dictionary_path = Path(dictionary_path)
    violin_pathogens_csv = Path(violin_pathogens_csv)
    if not dictionary_path.exists():
        raise FileNotFoundError(f"Dictionary not found: {dictionary_path}")
    if not violin_pathogens_csv.exists():
        raise FileNotFoundError(f"VIOLIN CSV not found: {violin_pathogens_csv}")

    # Wire the dictionary into the singleton lookup uses.
    # Reset first so a stale singleton from a different test doesn't leak in.
    import apecx_integration.synonym_dictionary.loader as _loader

    _loader._singleton = _ProcessSingleton()
    configure_dictionary_path(dictionary_path)

    df = pd.read_csv(violin_pathogens_csv)
    if max_rows is not None:
        df = df.head(max_rows)

    metrics = AccuracyMetrics(total_rows=len(df), rows_with_ground_truth=0)

    for _, row in df.iterrows():
        surface_form = row.get(surface_form_column)
        if not isinstance(surface_form, str) or not surface_form.strip():
            continue
        expected_iri = _build_expected_pathogen_iri(row.get(ground_truth_column))

        result = lookup_entity(surface_form, entity_type=EntityType.PATHOGEN)

        # Tally path
        if result.path == "fast":
            metrics.fast_count += 1
        elif result.path == "ancestor":
            metrics.ancestor_count += 1
        elif result.path == "slow":
            metrics.slow_count += 1
        else:
            metrics.miss_count += 1

        bucket = _confidence_bucket(result.confidence)
        metrics.confidence_buckets[bucket] = metrics.confidence_buckets.get(bucket, 0) + 1

        if expected_iri is None:
            # No ground truth → don't tally correctness (only path)
            continue
        metrics.rows_with_ground_truth += 1

        if result.canonical_iri is None:
            metrics.miss_with_truth_count += 1
            continue
        if result.canonical_iri == expected_iri:
            metrics.correct_count += 1
        else:
            metrics.incorrect_count += 1
            metrics.mismatches.append(
                Mismatch(
                    surface_form=surface_form,
                    expected_iri=expected_iri,
                    got_iri=result.canonical_iri,
                    got_path=result.path,
                    got_confidence=result.confidence,
                    got_label=result.canonical_label,
                )
            )

    return metrics
