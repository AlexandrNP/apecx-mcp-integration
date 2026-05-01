"""Stage 1 build pipeline — orchestrates resolvers + writer to produce
the dictionary artifact and per-row enriched CSVs.

The CLI in :mod:`apecx_integration.synonym_dictionary.cli` is a thin
wrapper around this module.

Pipeline shape:

1. For each (input_table, resolver) the caller registers, iterate rows.
2. Run the resolver to produce a :class:`ResolutionResult` per row.
3. Aggregate results into per-IRI :class:`DictionaryEntry` records
   (synonyms unioned across all source rows that anchored to the same IRI).
4. Emit enriched CSVs alongside the dictionary artifact.
5. Emit a :class:`BuildManifest` summarising the build.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
    ResolutionStatus,
)
from apecx_integration.synonym_dictionary.ols_client import OLSClient
from apecx_integration.synonym_dictionary.resolvers import _ResolverBase
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableSpec:
    """One input table to enrich during a build.

    Attributes
    ----------
    name:
        Identifier used in source-record provenance (e.g. ``"violin.pathogen"``).
    input_path:
        Path to the input CSV/TSV file.
    output_path:
        Where the enriched output is written.
    resolver_factory:
        Callable taking an :class:`OLSClient` + dictionary_version and
        returning a configured resolver.  The factory pattern lets the
        build keep one OLS client across all tables (so the cache works).
    sep:
        CSV separator (``,`` for VIOLIN, ``\\t`` for BV-BRC).
    """

    name: str
    input_path: Path
    output_path: Path
    resolver_factory: Any  # Callable[[OLSClient, str], _ResolverBase]
    sep: str = ","


@dataclass
class _IRIAggregate:
    """Mutable accumulator for one canonical IRI across multiple rows."""

    canonical_label: str
    ontology: OntologyName
    confidence: float
    resolved_at: datetime
    synonyms: set[str] = field(default_factory=set)
    source_records: list[str] = field(default_factory=list)


async def build_dictionary(
    *,
    table_specs: list[TableSpec],
    output_dictionary: Path,
    dictionary_version: str,
    ontology_versions: dict[OntologyName, str],
    writer_factory: Any,  # Callable[[Path], DictionaryWriter]
    ols_client: OLSClient | None = None,
) -> BuildManifest:
    """Run a full Stage 1 build.

    Parameters
    ----------
    table_specs:
        List of input tables to enrich.
    output_dictionary:
        Path of the SQLite dictionary artifact to produce.
    dictionary_version:
        Build identifier; goes into both the manifest and every per-row
        ``dictionary_version`` field.
    ontology_versions:
        Pinned per-ontology version strings, recorded in the manifest.
    writer_factory:
        Callable that takes ``output_dictionary`` and returns a
        :class:`DictionaryWriter`.  Indirection so tests can supply a
        fake writer.
    ols_client:
        Optional injected client.  If None, a default OLS client is
        constructed for the duration of the build.

    Returns
    -------
    The :class:`BuildManifest` written to the dictionary artifact.
    """
    output_dictionary = Path(output_dictionary)
    own_client = ols_client is None
    client = ols_client or OLSClient()

    # Aggregator keyed on (entity_type, canonical_iri).
    aggregates: dict[tuple[EntityType, str], _IRIAggregate] = {}
    counts: dict[EntityType, int] = {}
    unresolved_count = 0
    record_count_total = 0

    try:
        for spec in table_specs:
            resolver = spec.resolver_factory(client, dictionary_version)
            log.info("processing %s with %s", spec.name, type(resolver).__name__)
            n_rows, n_unresolved = await _process_table(
                spec=spec,
                resolver=resolver,
                aggregates=aggregates,
            )
            record_count_total += n_rows
            unresolved_count += n_unresolved
            counts[resolver.entity_type] = counts.get(resolver.entity_type, 0) + n_rows
    finally:
        if own_client:
            await client.close()

    # Emit dictionary entries + manifest.
    output_dictionary.parent.mkdir(parents=True, exist_ok=True)
    with writer_factory(output_dictionary) as writer:
        for (entity_type, iri), agg in aggregates.items():
            writer.write_entry(
                DictionaryEntry(
                    entity_type=entity_type,
                    canonical_iri=iri,
                    canonical_label=agg.canonical_label,
                    synonyms=tuple(sorted(agg.synonyms)),
                    ontology=agg.ontology,
                    ontology_version=ontology_versions.get(agg.ontology, "unknown"),
                    source_records=tuple(agg.source_records),
                    confidence=agg.confidence,
                    resolved_at=agg.resolved_at,
                )
            )

        manifest = BuildManifest(
            dictionary_version=dictionary_version,
            built_at=datetime.now(UTC),
            ontology_versions={k.value: v for k, v in ontology_versions.items()},
            record_counts_per_entity_type=counts,
            unresolved_count=unresolved_count,
            record_count_total=record_count_total,
        )
        writer.write_manifest(manifest)

    return manifest


async def _process_table(
    *,
    spec: TableSpec,
    resolver: _ResolverBase,
    aggregates: dict[tuple[EntityType, str], _IRIAggregate],
) -> tuple[int, int]:
    """Resolve every row of ``spec.input_path`` and write the enriched
    output.  Returns (rows_processed, rows_unresolved)."""
    df = pd.read_csv(spec.input_path, sep=spec.sep, low_memory=False)
    n_unresolved = 0
    enriched_rows: list[dict[str, Any]] = []

    for idx, raw_row in df.iterrows():
        # Strip pandas NaN -> None to keep resolvers clean.
        record = {k: (None if pd.isna(v) else v) for k, v in raw_row.items()}
        result = await resolver.resolve(record)

        if result.resolution_status == ResolutionStatus.UNRESOLVED:
            n_unresolved += 1
        else:
            assert result.canonical_iri is not None
            assert result.canonical_label is not None
            assert result.canonical_ontology is not None
            key = (resolver.entity_type, result.canonical_iri)
            agg = aggregates.get(key)
            if agg is None:
                agg = _IRIAggregate(
                    canonical_label=result.canonical_label,
                    ontology=result.canonical_ontology,
                    confidence=result.resolution_confidence,
                    resolved_at=datetime.now(UTC),
                )
                aggregates[key] = agg
            agg.synonyms.update(result.synonyms)
            # Add the source-row label as a synonym too — that's what the
            # user typed, by definition; future lookups should hit it.
            for label_field in (
                "Pathogen",
                "Vaccine_Name",
                "Vaccine",
                "Disease",
                "Gene_Name",
                "genome_name",
                "genome.genome_name",
            ):
                src_label = record.get(label_field)
                if isinstance(src_label, str) and src_label.strip():
                    agg.synonyms.add(src_label.strip())
                    break
            agg.source_records.append(f"{spec.name}.{idx}")

        enriched_rows.append(
            {
                **record,
                "canonical_iri": result.canonical_iri,
                "canonical_label": result.canonical_label,
                "canonical_ontology": (
                    result.canonical_ontology.value
                    if result.canonical_ontology is not None
                    else None
                ),
                "resolution_status": result.resolution_status.value,
                "resolution_confidence": result.resolution_confidence,
                "dictionary_version": result.dictionary_version,
            }
        )

    # Write enriched CSV.
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)
    if enriched_rows:
        fieldnames = list(enriched_rows[0].keys())
        with spec.output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(enriched_rows)

    return len(enriched_rows), n_unresolved


def run_build_sync(**kwargs: Any) -> BuildManifest:
    """Synchronous wrapper around :func:`build_dictionary` for CLI usage."""
    return asyncio.run(build_dictionary(**kwargs))
