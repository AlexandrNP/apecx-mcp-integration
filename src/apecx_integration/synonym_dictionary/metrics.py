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


def _build_expected_vaccine_iri(vo_id: float | int | str | None) -> str | None:
    """Convert a VIOLIN ``Vaccine_Ontology_ID`` cell to an OBO IRI, or None.

    Source values are like "VO_0000003" or "3"; we accept either and emit
    the canonical OBO form. Padding is consistent with the build-time IRI
    construction in ``normalize_iri(prefix='VO_')``.
    """
    if vo_id is None or pd.isna(vo_id):
        return None
    s = str(vo_id).strip()
    if not s:
        return None
    # Strip "VO_" / "VO:" prefix if present so we can re-zero-pad.
    if s.upper().startswith("VO_") or s.upper().startswith("VO:"):
        s = s[3:]
    try:
        as_int = int(float(s))
    except (TypeError, ValueError):
        return None
    return f"http://purl.obolibrary.org/obo/VO_{as_int:07d}"


def _build_expected_gene_iri(gene_id: float | int | str | None) -> str | None:
    """Convert a VIOLIN ``NCBI_Gene_ID`` cell to an identifiers.org IRI."""
    if gene_id is None or pd.isna(gene_id):
        return None
    try:
        as_int = int(float(gene_id))
    except (TypeError, ValueError):
        return None
    return f"http://identifiers.org/ncbigene/{as_int}"


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


def _measure_accuracy(
    dictionary_path: Path | str,
    csv_path: Path | str,
    *,
    entity_type: EntityType,
    surface_form_column: str,
    ground_truth_column: str | None,
    expected_iri_builder,
    max_rows: int | None = None,
    surface_form_fallback_columns: tuple[str, ...] = (),
) -> AccuracyMetrics:
    """Generic accuracy measurement core. Use the public wrappers.

    Parameters
    ----------
    dictionary_path:
        Path to a built ``dictionary.sqlite`` artifact.
    csv_path:
        Path to a CSV with at least ``surface_form_column``.
    entity_type:
        Restricts the lookup to one EntityType (passed to ``lookup_entity``).
    surface_form_column:
        Primary column carrying the user-typed name.
    surface_form_fallback_columns:
        Additional columns checked in order if the primary is empty/null.
        Useful for VIOLIN tables where the canonical surface column varies
        by row (e.g., ``Vaccine`` vs ``Vaccine_Name``).
    ground_truth_column:
        Column carrying the expected ID. ``None`` skips correctness scoring
        (used for entity types without an in-CSV ground-truth column,
        e.g. Disease).
    expected_iri_builder:
        Callable mapping the ground-truth cell value to an expected IRI.

    Behavior
    --------
    Resets the process singleton, configures it to ``dictionary_path``,
    iterates the CSV, and tallies path / correctness / confidence
    distribution into an :class:`AccuracyMetrics`.
    """
    dictionary_path = Path(dictionary_path)
    csv_path = Path(csv_path)
    if not dictionary_path.exists():
        raise FileNotFoundError(f"Dictionary not found: {dictionary_path}")
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    # Reset + configure the singleton each call so stale state from another
    # test doesn't leak in.
    import apecx_integration.synonym_dictionary.loader as _loader

    _loader._singleton = _ProcessSingleton()
    configure_dictionary_path(dictionary_path)

    df = pd.read_csv(csv_path)
    if max_rows is not None:
        df = df.head(max_rows)

    metrics = AccuracyMetrics(total_rows=len(df), rows_with_ground_truth=0)

    for _, row in df.iterrows():
        # Pull surface form, falling back through optional alt columns.
        surface_form = row.get(surface_form_column)
        if not isinstance(surface_form, str) or not surface_form.strip():
            for alt in surface_form_fallback_columns:
                v = row.get(alt)
                if isinstance(v, str) and v.strip():
                    surface_form = v
                    break
        if not isinstance(surface_form, str) or not surface_form.strip():
            continue

        expected_iri = (
            expected_iri_builder(row.get(ground_truth_column))
            if ground_truth_column is not None
            else None
        )

        result = lookup_entity(surface_form, entity_type=entity_type)

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


def measure_pathogen_accuracy(
    dictionary_path: Path | str,
    violin_pathogens_csv: Path | str,
    *,
    max_rows: int | None = None,
) -> AccuracyMetrics:
    """Pathogen IRI accuracy against VIOLIN ``Pathogen_Information``."""
    return _measure_accuracy(
        dictionary_path,
        violin_pathogens_csv,
        entity_type=EntityType.PATHOGEN,
        surface_form_column="Pathogen",
        ground_truth_column="NCBI_Taxonomy_ID",
        expected_iri_builder=_build_expected_pathogen_iri,
        max_rows=max_rows,
    )


def measure_vaccine_accuracy(
    dictionary_path: Path | str,
    violin_vaccines_csv: Path | str,
    *,
    max_rows: int | None = None,
) -> AccuracyMetrics:
    """Vaccine IRI accuracy against VIOLIN ``Vaccine_Information``.

    Surface form falls back ``Vaccine_Name`` → ``Vaccine`` to match the
    resolver's own source-column priority.
    """
    return _measure_accuracy(
        dictionary_path,
        violin_vaccines_csv,
        entity_type=EntityType.VACCINE,
        surface_form_column="Vaccine_Name",
        surface_form_fallback_columns=("Vaccine",),
        ground_truth_column="Vaccine_Ontology_ID",
        expected_iri_builder=_build_expected_vaccine_iri,
        max_rows=max_rows,
    )


def measure_gene_accuracy(
    dictionary_path: Path | str,
    violin_genes_csv: Path | str,
    *,
    max_rows: int | None = None,
) -> AccuracyMetrics:
    """Gene IRI accuracy against VIOLIN ``Gene_Information``.

    NCBI Gene IRIs use identifiers.org canonical form, not OBO.
    """
    return _measure_accuracy(
        dictionary_path,
        violin_genes_csv,
        entity_type=EntityType.GENE,
        surface_form_column="Gene_Name",
        ground_truth_column="NCBI_Gene_ID",
        expected_iri_builder=_build_expected_gene_iri,
        max_rows=max_rows,
    )


def measure_disease_path_distribution(
    dictionary_path: Path | str,
    violin_pathogens_csv: Path | str,
    *,
    max_rows: int | None = None,
) -> AccuracyMetrics:
    """Disease lookup metrics from the ``Disease`` column of Pathogen rows.

    No ground-truth DOID column exists in VIOLIN, so this returns
    path-distribution + confidence stats only. Recall/precision will
    show 0/1.0 trivially (no rows have ground truth) — the useful signal
    is fast/slow/miss counts.
    """
    return _measure_accuracy(
        dictionary_path,
        violin_pathogens_csv,
        entity_type=EntityType.DISEASE,
        surface_form_column="Disease",
        ground_truth_column=None,
        expected_iri_builder=lambda _: None,
        max_rows=max_rows,
    )
