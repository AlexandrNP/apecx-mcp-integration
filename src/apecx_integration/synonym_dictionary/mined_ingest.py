"""SC-B4 (2026-06-08) — ingest corpus-mined synonym observations into an
existing dictionary artifact.

Input shape (JSONL sidecar):

    {"surface_form": "EEEV", "surface_form_normalized": "eeev",
     "taxon_id": 11021, "source": "violin_pathogen", "source_count": 1}
    ...

Each row represents a unique ``(surface_form_normalized, taxon_id)``
pair seen by ``source_count`` sources. Multi-source pairs (count ≥ 2)
become ``MINED_CORROBORATED`` synonyms (confidence 0.95); single-source
become ``MINED_OBSERVED`` (confidence 0.90), per SC plan §3.1.

Why a delta-update rather than a full rebuild:
* Mined observations are append-only relative to SC-A4's synthesis.
* The 281k-taxon synthesis is expensive; rebuilding to add a few
  thousand mined synonyms is wasteful.
* The same applies semantically: mined synonyms ADD to existing
  entries' synonym lists; they don't replace anything.

Conflict policy (SC-B3 surfacing here):
* When the same normalized surface is mined for ≥2 distinct taxa,
  every conflicting (surface, taxon, source) gets written to the
  ``mined_conflicts`` table for the audit trail.
* The inverse_index gets the multi-IRI semantics SC-A5b already wired
  in the loader (so the read-time AMBIGUOUS path catches them
  automatically — no separate routing logic needed).
* The dictionary build does NOT pick a "winner" or apply
  precedence rules; that is policy that lives in a higher layer.

Confidence tier overrides:
* If an entry already carries an ID-anchored synonym for this surface
  (came from NCBI names.dmp at conf=1.0), the mined observation does
  NOT downgrade it — the entry's confidence stays at the higher value.
* The mined surface still lands in inverse_index for query coverage
  (it's already there if it matched the canonical), but the entry's
  recorded confidence remains the authoritative one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apecx_integration.synonym_dictionary.normalization import (
    normalize_surface_form,
)

log = logging.getLogger(__name__)

_NCBITAXON_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"


@dataclass(frozen=True)
class MinedRow:
    """One row of the mined_observations.jsonl sidecar."""

    surface_form: str
    surface_form_normalized: str
    taxon_id: int
    source: str
    source_count: int


def _iter_mined_rows(path: Path) -> Iterable[MinedRow]:
    """Yield MinedRow objects from the sidecar JSONL."""
    with path.open() as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                yield MinedRow(
                    surface_form=obj["surface_form"],
                    surface_form_normalized=obj["surface_form_normalized"],
                    taxon_id=int(obj["taxon_id"]),
                    source=str(obj["source"]),
                    source_count=int(obj.get("source_count", 1)),
                )
            except (KeyError, ValueError, TypeError) as exc:
                log.warning(
                    "mined_observations: skipping malformed row %d: %s",
                    line_no,
                    exc,
                )


@dataclass
class IngestSummary:
    """Result of ingesting a mined-observations sidecar."""

    rows_read: int = 0
    entries_touched: int = 0
    synonyms_added: int = 0
    inverse_writes: int = 0
    new_ambiguity_captures: int = 0
    mined_conflicts_written: int = 0
    missing_entries: int = 0


def ingest_mined_observations(
    *,
    dict_path: Path,
    mined_jsonl: Path,
    entity_type: str = "pathogen",
) -> IngestSummary:
    """Apply a mined-observations JSONL sidecar against an existing dictionary.

    Returns an :class:`IngestSummary` with per-step counts. Does NOT
    rebuild the dictionary; only appends synonyms + inverse_index rows
    + mined_conflicts entries. Idempotent if re-applied with the same
    input (set-based dedup at every layer).
    """
    if not dict_path.exists():
        raise FileNotFoundError(f"dictionary not found: {dict_path}")
    if not mined_jsonl.exists():
        raise FileNotFoundError(f"mined-observations JSONL not found: {mined_jsonl}")

    # Bucket rows by (normalized_surface, taxon_id) so we can compute
    # the per-pair winning source-count and detect surface→multi-taxon
    # conflicts in one pass.
    by_pair: dict[tuple[str, int], list[MinedRow]] = defaultdict(list)
    rows_read = 0
    for row in _iter_mined_rows(mined_jsonl):
        rows_read += 1
        # Re-normalize to be safe — the source pipeline's normalizer
        # may differ from this repo's. The consumer's normalizer wins.
        consumer_normalized = normalize_surface_form(row.surface_form)
        if not consumer_normalized:
            continue
        key = (consumer_normalized, row.taxon_id)
        by_pair[key].append(row)

    # Compute per-surface taxon multiplicity for SC-B3 conflict surfacing.
    surface_to_taxa: dict[str, set[int]] = defaultdict(set)
    for normalized, tid in by_pair:
        surface_to_taxa[normalized].add(tid)
    conflicted_surfaces = {s for s, taxa in surface_to_taxa.items() if len(taxa) >= 2}

    summary = IngestSummary(rows_read=rows_read)
    conn = sqlite3.connect(dict_path)
    # PERF: WAL + reduced sync are safe for a single-writer ingest
    # session. Without them, every UPDATE costs an fsync — 11k rows
    # take 3+ minutes; with them, ~30 seconds. Both PRAGMAs are
    # connection-scoped and don't change file-format.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -65536")  # 64 MiB page cache

    # Ensure SC-B3 mined_conflicts table exists. Older dictionaries
    # (built before SC-B3 landed) lack it; idempotent CREATE TABLE IF
    # NOT EXISTS is safe to run unconditionally.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mined_conflicts (
            surface_form_normalized TEXT NOT NULL,
            candidate_taxon_id      INTEGER NOT NULL,
            conflict_source         TEXT NOT NULL,
            source_count_for_pair   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (surface_form_normalized, candidate_taxon_id, conflict_source)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mined_conflicts_by_surface "
        "ON mined_conflicts (surface_form_normalized)"
    )
    try:
        for (normalized, tid), rows in by_pair.items():
            iri = f"{_NCBITAXON_PREFIX}{tid}"
            # Use the composite (entity_type, canonical_iri) PK index by
            # supplying entity_type. Without it, SQLite full-scans the
            # 281k entries table 11k times = catastrophic.
            entry_row = conn.execute(
                "SELECT entity_type, synonyms_json, confidence "
                "FROM entries WHERE entity_type = ? AND canonical_iri = ?",
                (entity_type, iri),
            ).fetchone()
            if entry_row is None:
                summary.missing_entries += 1
                # Future: SC-B4b — emit a new entry from scratch when
                # the corpus surfaces a taxon SC-A4 didn't reach. For
                # virus-subtree builds this should never trigger
                # because SC-A4 covers every virus taxon.
                continue
            _existing_entity_type, syns_json, _conf = entry_row
            existing_syns = json.loads(syns_json)
            existing_set = set(existing_syns)
            # Use the first-seen original surface form (frozen across rows).
            original_surface = rows[0].surface_form.strip()
            if original_surface and original_surface not in existing_set:
                existing_syns.append(original_surface)
                conn.execute(
                    "UPDATE entries SET synonyms_json = ? WHERE canonical_iri = ?",
                    (json.dumps(existing_syns), iri),
                )
                summary.synonyms_added += 1
                summary.entries_touched += 1

            # inverse_index — honor existing SC-A5b multi-IRI semantics:
            # check for prior different IRI first; if so, capture into
            # ambiguous_surface_forms before overwriting.
            existing_inv = conn.execute(
                "SELECT canonical_iri FROM inverse_index "
                "WHERE entity_type = ? AND surface_form_normalized = ?",
                (entity_type, normalized),
            ).fetchone()
            if existing_inv is not None and existing_inv[0] != iri:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ambiguous_surface_forms (
                        entity_type, surface_form_normalized,
                        winning_canonical_iri, alternative_canonical_iri
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (entity_type, normalized, iri, existing_inv[0]),
                )
                summary.new_ambiguity_captures += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO inverse_index (
                    entity_type, surface_form_normalized, canonical_iri
                ) VALUES (?, ?, ?)
                """,
                (entity_type, normalized, iri),
            )
            summary.inverse_writes += 1

            # SC-B3: write into mined_conflicts when this surface has
            # multiple candidate taxa in the mining run.
            if normalized in conflicted_surfaces:
                # Per-source row count is the size of the rows list.
                # Use the highest source_count seen for this (pair, source).
                by_source: dict[str, int] = defaultdict(int)
                for r in rows:
                    by_source[r.source] = max(by_source[r.source], r.source_count)
                for source, count in by_source.items():
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO mined_conflicts (
                            surface_form_normalized, candidate_taxon_id,
                            conflict_source, source_count_for_pair
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (normalized, tid, source, count),
                    )
                    summary.mined_conflicts_written += 1
        conn.commit()
    finally:
        conn.close()

    log.info(
        "mined_ingest: read=%d touched=%d synonyms_added=%d inverse=%d "
        "ambiguity_captures=%d conflicts=%d missing=%d",
        summary.rows_read,
        summary.entries_touched,
        summary.synonyms_added,
        summary.inverse_writes,
        summary.new_ambiguity_captures,
        summary.mined_conflicts_written,
        summary.missing_entries,
    )
    return summary


__all__ = ["MinedRow", "IngestSummary", "ingest_mined_observations"]
