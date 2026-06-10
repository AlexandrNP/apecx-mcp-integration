"""Stage 1 build pipeline — orchestrates resolvers + writer to produce
the dictionary artifact and per-row enriched CSVs.

This module is wrapped by :class:`apecx_integration.synonym_dictionary.workflow.dictionary_build_step.DictionaryBuildStep`.
End users do not invoke it directly — the build runs as part of the
nanobrain ``dictionary_build_workflow``, triggered lazily at apecx-mcp
startup (see ``synonym_dictionary.workflow.bootstrap.ensure_dictionary``).

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
import re
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
    nodes_dmp_path: Path | None = None,
    merged_dmp_path: Path | None = None,
    names_dmp_path: Path | None = None,
    delnodes_dmp_path: Path | None = None,
    taxonomy_subtree_root: int | None = None,
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
    nodes_dmp_path:
        Optional path to NCBI Taxonomy ``nodes.dmp``.  When supplied,
        the full ``taxon_hierarchy`` table is written into the SQLite
        artifact, enabling Stage 2 ancestor traversal.
    merged_dmp_path:
        Optional path to NCBI Taxonomy ``merged.dmp``.  When supplied,
        deprecated taxon ID remappings are stored alongside the hierarchy.
    names_dmp_path:
        Optional path to NCBI Taxonomy ``names.dmp``. **Added 2026-06-08
        (SC-A4).** When supplied with ``taxonomy_subtree_root``, a
        synthetic dictionary entry is emitted for every taxon in the
        subtree carrying the 7 ingested name classes (see
        :data:`hierarchy_loader.NCBI_NAME_CLASSES_INGESTED`) as
        synonyms. Previously, dictionary entries only existed for IRIs
        the corpus had resolved; this expands coverage to taxa the
        corpus never touched (e.g., the user types ``"EEEV"`` even when
        no VIOLIN/BVBRC row carries it). Authoritative confidence (1.0)
        per D7 of SYNONYM_COMPLETENESS_PLAN — overrides any corpus-set
        confidence for the same IRI.
    delnodes_dmp_path:
        Optional path to NCBI Taxonomy ``delnodes.dmp``. **Added 2026-06-08
        (SC-A4).** When supplied, deleted taxon ids are persisted via
        :meth:`DictionaryWriter.write_deleted_taxons` so lookups can
        return a loud ``unresolved`` with ``evidence = "taxon deleted"``
        instead of a silent miss.
    taxonomy_subtree_root:
        NCBI taxon id used as the root for the names.dmp synthesis scope
        (e.g., ``10239`` for Viruses). Required when ``names_dmp_path``
        is supplied. Without a root, synthesizing entries for all 2.7M
        NCBI taxa would balloon the SQLite artifact ~50× (see
        ``SYNONYM_COMPLETENESS_PLAN.md`` §SC-A7).

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
    synthesized_subtree_count = 0

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

    # SC-A4 synthesis: expand aggregates with one entry per taxon in the
    # configured NCBI Taxonomy subtree, using all 7 ingested name classes
    # as synonyms. The build no longer depends on the corpus having seen a
    # taxon for that taxon to be queryable; "EEEV" resolves even when no
    # source row resolved to NCBITaxon:11036.
    if names_dmp_path is not None:
        if nodes_dmp_path is None:
            raise ValueError(
                "build_dictionary: names_dmp_path was supplied without "
                "nodes_dmp_path — the names.dmp synthesis pass needs the "
                "taxonomy hierarchy to scope to the subtree."
            )
        if taxonomy_subtree_root is None:
            raise ValueError(
                "build_dictionary: names_dmp_path was supplied without "
                "taxonomy_subtree_root — refusing to synthesize entries "
                "for the full 2.7M-taxon NCBI tree by default. Set "
                "taxonomy_subtree_root=10239 for the virus subtree."
            )
        synthesized_subtree_count = _synthesize_subtree_entries(
            aggregates=aggregates,
            nodes_dmp_path=nodes_dmp_path,
            names_dmp_path=names_dmp_path,
            taxonomy_subtree_root=taxonomy_subtree_root,
            ontology_version=ontology_versions.get(OntologyName.NCBITAXON, "unknown"),
        )
        log.info(
            "names.dmp synthesis: %d taxa in subtree root %d expanded into aggregates",
            synthesized_subtree_count,
            taxonomy_subtree_root,
        )

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

        # Optional: embed the NCBITaxon hierarchy for Stage 2 ancestor traversal.
        if nodes_dmp_path is not None:
            from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: PLC0415
                parse_nodes_dmp,
            )

            log.info("writing taxon_hierarchy from %s", nodes_dmp_path)
            writer.write_taxon_hierarchy(parse_nodes_dmp(nodes_dmp_path))

        if merged_dmp_path is not None:
            from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: PLC0415
                parse_merged_dmp,
            )

            log.info("writing merged_taxons from %s", merged_dmp_path)
            writer.write_merged_taxons(parse_merged_dmp(merged_dmp_path))

        if delnodes_dmp_path is not None:
            from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: PLC0415
                parse_delnodes_dmp,
            )

            log.info("writing deleted_taxons from %s", delnodes_dmp_path)
            writer.write_deleted_taxons(parse_delnodes_dmp(delnodes_dmp_path))

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


_NCBITAXON_IRI_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"


# Pattern for extracting all-caps acronyms that appear as the **terminal
# token** of an NCBI ``equivalent name`` / ``synonym`` string. NCBI's
# curation pattern is "<long name> <ACRONYM>":
#
#   11021 | eastern equine encephalomyelitis virus EEEV |  | equivalent name |
#   11021 | eastern equine encephalomyelitis EEE        |  | equivalent name |
#
# Without this extraction, ``EEEV`` does not appear in the dictionary
# even though NCBI ships it — the design-doc assumption that the 7 name
# classes alone cover acronyms was overconfident.
#
# **Terminal-token anchor is load-bearing**: an earlier draft used an
# unanchored ``\b[A-Z][A-Z0-9]{2,7}\b`` and discovered that strain-level
# scientific synonyms like ``"Influenza A virus (A/duck/.../1982(H1N1))"``
# embed ``H1N1`` in the middle of 36,201 different strain taxon names —
# the resulting last-write-wins inverse-index entry for ``"h1n1"`` ended
# up pointing at a random strain isolate, NOT the H1N1 subtype taxon
# 114727. The terminal-anchor pattern (acronym as the LAST whitespace-
# separated token) cleanly catches NCBI's suffix-curation style while
# excluding the parenthetical strain-detail case.
#
# 3-character minimum, 8-char maximum: 3 avoids single-letter noise;
# 8 filters out strain serial numbers. Applied ONLY to equivalent-name
# / synonym rows; scientific names (which often embed strain identifiers
# like ``"CHIKV/IRL/2007"``) are left alone.
_EMBEDDED_ACRONYM_RE = re.compile(r"(?:^|\s)([A-Z][A-Z0-9]{2,7})\s*$")
_EMBEDDED_ACRONYM_CLASSES: frozenset[str] = frozenset({"equivalent name", "synonym"})


def _synthesize_subtree_entries(
    *,
    aggregates: dict[tuple[EntityType, str], _IRIAggregate],
    nodes_dmp_path: Path,
    names_dmp_path: Path,
    taxonomy_subtree_root: int,
    ontology_version: str,
) -> int:
    """Expand ``aggregates`` with one (PATHOGEN, NCBITaxon-IRI) entry per
    taxon in the configured subtree.

    For each taxon in the subtree:

    - Synonyms = the union of all ``name_text`` values whose ``name_class``
      is in :data:`NCBI_NAME_CLASSES_INGESTED`.
    - Canonical label = the ``scientific name``.
    - Confidence = 1.0 (authoritative).
    - If the corpus already created an aggregate for the same IRI under
      ``EntityType.PATHOGEN``, names.dmp synonyms merge in and confidence
      is bumped to 1.0 (D7: authoritative wins ties). The corpus's
      ``canonical_label`` is preserved (the OLS-resolved label is the
      consumer-facing display name; names.dmp's scientific name is the
      synonym-set authority).
    - Aggregates the corpus stored under non-PATHOGEN entity_types (e.g.
      GENOME for BVBRC genome-table rows that resolve to the same NCBI
      taxon) are left untouched. The synthesized PATHOGEN entry is a
      separate row by design — queries with entity_type=PATHOGEN or
      entity_type=None hit it; entity_type=GENOME queries continue to
      route through the corpus entry.

    Returns the number of (taxon → entry) synthesizing operations
    performed; useful for the build manifest.
    """
    from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: PLC0415
        NCBI_NAME_CLASSES_INGESTED,
        compute_subtree_descendants,
        parse_names_dmp,
    )

    log.info(
        "computing subtree descendants from %s rooted at NCBITaxon:%d",
        nodes_dmp_path,
        taxonomy_subtree_root,
    )
    subtree = compute_subtree_descendants(nodes_dmp_path, taxonomy_subtree_root)
    log.info("subtree size: %d taxa", len(subtree))

    # First pass: gather scientific names + synonyms per in-scope taxon.
    # Buffering in memory keeps the writer pass simple; the virus subtree
    # at ~10k taxa × a few names each is well under 1 MB.
    per_taxon_scientific: dict[int, str] = {}
    per_taxon_synonyms: dict[int, set[str]] = {}
    embedded_acronyms_lifted = 0
    for record in parse_names_dmp(names_dmp_path):
        if record.taxon_id not in subtree:
            continue
        if record.name_class not in NCBI_NAME_CLASSES_INGESTED:
            continue
        if record.name_class == "scientific name":
            # First scientific name wins; NCBI ships exactly one per taxon
            # but be defensive.
            per_taxon_scientific.setdefault(record.taxon_id, record.name_text)
        per_taxon_synonyms.setdefault(record.taxon_id, set()).add(record.name_text)

        # Extract embedded all-caps acronyms (e.g. ``EEEV`` inside
        # ``"eastern equine encephalomyelitis virus EEEV"``). NCBI
        # under-curates these as standalone ``acronym`` rows.
        if record.name_class in _EMBEDDED_ACRONYM_CLASSES:
            for token in _EMBEDDED_ACRONYM_RE.findall(record.name_text):
                if token in per_taxon_synonyms[record.taxon_id]:
                    continue
                per_taxon_synonyms[record.taxon_id].add(token)
                embedded_acronyms_lifted += 1

    if embedded_acronyms_lifted:
        log.info(
            "embedded-acronym extraction: lifted %d additional surface forms "
            "(e.g. 'EEEV' from 'eastern equine encephalomyelitis virus EEEV')",
            embedded_acronyms_lifted,
        )

    now = datetime.now(UTC)
    synthesized = 0
    for taxon_id, synonyms in per_taxon_synonyms.items():
        scientific = per_taxon_scientific.get(taxon_id)
        if scientific is None:
            # A taxon with synonyms but no scientific name is malformed;
            # skip rather than fabricate a label.
            log.debug(
                "skipping NCBITaxon:%d — has name_class entries but no "
                "scientific name; not synthesizing",
                taxon_id,
            )
            continue
        iri = f"{_NCBITAXON_IRI_PREFIX}{taxon_id}"
        key = (EntityType.PATHOGEN, iri)
        agg = aggregates.get(key)
        if agg is None:
            aggregates[key] = _IRIAggregate(
                canonical_label=scientific,
                ontology=OntologyName.NCBITAXON,
                confidence=1.0,
                resolved_at=now,
                synonyms=set(synonyms),
                source_records=[f"ncbi_names_dmp.{taxon_id}"],
            )
        else:
            agg.synonyms.update(synonyms)
            agg.confidence = 1.0  # authoritative wins ties (D7).
            agg.source_records.append(f"ncbi_names_dmp.{taxon_id}")
        synthesized += 1
    return synthesized


def run_build_sync(**kwargs: Any) -> BuildManifest:
    """Synchronous wrapper around :func:`build_dictionary` for CLI usage."""
    return asyncio.run(build_dictionary(**kwargs))
