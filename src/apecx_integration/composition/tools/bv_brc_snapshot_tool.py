"""``BVBRCSnapshotTool`` — reads from local BV-BRC TSV/FASTA snapshots
instead of hitting the live BV-BRC REST API (T02 Phase 4).

## Why

The T02 workflow spec (``docs/workflow_spec.md``) calls for local-
default execution on a scientist's laptop. The existing
``BVBRCTool`` in nanobrain makes HTTP calls to
``https://www.bv-brc.org/api``; every workflow run hammering the
public API is unnecessary latency and doesn't survive air-gapped
environments. The scientist gets a snapshot of the relevant
pathogen's BV-BRC data at session start (stored under
``data/bvbrc_cache/*.tsv`` + ``*.fasta``) and the snapshot tool
serves every workflow from that static cache.

## How

Subclasses ``BVBRCTool`` and overrides the three data-fetching
methods that actually produce results. Inherits everything else
(config schema, batch-size fields, from_config pattern). The
existing workflow steps (``enhanced_bv_brc_data_acquisition_step``
Step 2, ``bv_brc_data_acquisition_step`` Step 6) only read
batch-size fields from the config and instantiate the tool; they
don't introspect its type, so the subclass slots in transparently.

## Snapshot layout

- ``<snapshot_dir>/alphavirus_genomes.tsv`` — 2 columns:
  ``genome.genome_id`` + ``genome.genome_name``.
- ``<snapshot_dir>/alphavirus_proteins.tsv`` — 5 columns:
  ``genome.genome_id`` + ``feature.patric_id`` + ``feature.product``
  + ``feature.aa_sequence_md5`` + ``feature.genome_id``.
- ``<snapshot_dir>/alphavirus_proteins_annotated.fasta`` — amino-acid
  sequences keyed by md5 in the FASTA description line.

``<snapshot_dir>`` defaults to ``data/bvbrc_cache`` (CWD-relative)
and overrides via env var ``APECX_BVBRC_SNAPSHOT_DIR``.

## What the snapshot CANNOT do

- **Size-based filtering**. The snapshot TSV lacks a ``genome_length``
  column (it's a subset of what the live API returns). The
  ``filter_genomes_by_size`` override passes all genomes through
  unchanged and logs the gap. If the workflow ever needs real
  size-filtering, regenerate the snapshot with the genome_length
  column.
- **Taxonomy lookups** (``get_proteins_for_virus`` variants). Not
  overridden; if a caller invokes them they fall through to the
  base ``BVBRCTool`` which tries to hit the HTTP API. Intentional:
  those paths are outside the T02 workflow's first release.

## Fail-loud contract

``download_alphavirus_genomes`` raises ``FileNotFoundError`` with
the resolved absolute path when the snapshot TSV is missing. This
is a deliberate failure mode — the scientist's snapshot should be
there; if it isn't, something is wrong with their setup.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path
from typing import Any

from nanobrain.library.tools.bioinformatics.bv_brc_tool import (
    BVBRCTool,
    GenomeData,
    ProteinData,
)

log = logging.getLogger(__name__)

SNAPSHOT_ENV_VAR = "APECX_BVBRC_SNAPSHOT_DIR"
DEFAULT_SNAPSHOT_DIR = "data/bvbrc_cache"

GENOMES_TSV_BASENAME = "alphavirus_genomes.tsv"
PROTEINS_TSV_BASENAME = "alphavirus_proteins.tsv"
PROTEINS_FASTA_BASENAME = "alphavirus_proteins_annotated.fasta"


def _resolve_snapshot_dir() -> Path:
    """Resolve the snapshot directory from env + default."""
    return Path(os.environ.get(SNAPSHOT_ENV_VAR, DEFAULT_SNAPSHOT_DIR))


def _read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(
            f"BVBRCSnapshotTool: snapshot file not found at {path.resolve()}. "
            f"Expected the scientist's pathogen snapshot under "
            f"``{_resolve_snapshot_dir()}`` (override via "
            f"{SNAPSHOT_ENV_VAR}). If this is a fresh clone, run the "
            f"snapshot-refresh workflow (Step 0 in the T00.1b spec — currently "
            f"deferred by the HARD-synonym directive; for now restore from "
            f"backup or copy snapshots from another machine)."
        )
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(dict(row))
    return rows


_MD5_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _extract_md5_from_header(line: str) -> str | None:
    """Extract a 32-char-hex md5 from a FASTA header line.

    Two header formats are accepted (BV-BRC snapshot data uses both
    over time):

    1. **Pipe-delimited** (real ``alphavirus_proteins_annotated.fasta``
       format observed 2026-04-26): the md5 is a pipe-delimited
       field on the header. Shape::

           >fig_<genome>.<feature_type>.<n>|<product>|<source>|<md5_hex>

    2. **md5= token** (older BV-BRC export shape; documented in the
       original module docstring)::

           >fig|<genome_id>.peg.<n> <product> [md5=<md5_hex>]

    Returns the md5 hex string when found, else None. Cluster AQ
    (2026-04-26): the original parser only handled format 2 and
    silently skipped 100% of real BV-BRC headers, producing empty
    protein-sequence tables and downstream workflow corruption.
    """
    body = line[1:].strip()
    # Format 1: pipe-delimited tail. Pick the last field that looks
    # like a 32-hex md5; tolerates additional metadata fields appended
    # after the md5 in future exports.
    if "|" in body:
        for part in reversed(body.split("|")):
            stripped = part.strip()
            if _MD5_HEX_RE.match(stripped):
                return stripped
    # Format 2: md5= token (whitespace-separated; bracket-stripped).
    for token in body.split():
        if token.startswith("md5=") and len(token) > 4:
            candidate = token[4:].strip("]").strip(",").strip()
            if candidate:
                return candidate
    return None


def _read_fasta_by_md5(path: Path) -> dict[str, str]:
    """Parse the annotated FASTA into ``{md5_hex: aa_sequence}``.

    Header parsing delegates to ``_extract_md5_from_header`` to support
    both the pipe-delimited BV-BRC snapshot format AND the older
    md5=<hex> token format. Headers that match neither are logged
    and skipped — a snapshot whose headers carry no md5 surfaces as
    a warning rather than a silent miss.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"BVBRCSnapshotTool: annotated FASTA not found at {path.resolve()}."
        )
    sequences: dict[str, str] = {}
    current_md5: str | None = None
    current_chunks: list[str] = []
    skipped_headers = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith(">"):
                if current_md5 is not None and current_chunks:
                    sequences[current_md5] = "".join(current_chunks)
                current_chunks = []
                current_md5 = _extract_md5_from_header(line)
                if current_md5 is None:
                    skipped_headers += 1
            else:
                current_chunks.append(line)
        if current_md5 is not None and current_chunks:
            sequences[current_md5] = "".join(current_chunks)
    if skipped_headers:
        log.warning(
            "BVBRCSnapshotTool: %d FASTA headers in %s carried no recognizable "
            "md5 (neither pipe-delimited tail nor md5= token); skipped their "
            "sequences. Check snapshot format.",
            skipped_headers,
            path.name,
        )
    return sequences


class BVBRCSnapshotTool(BVBRCTool):
    """Snapshot-aware variant of ``BVBRCTool``. Overrides the three
    live-API methods to read from local TSV/FASTA snapshots.
    Everything else (from_config pattern, batch-size config fields,
    progressive-scaling mixin) is inherited unchanged.
    """

    @property
    def snapshot_dir(self) -> Path:
        return _resolve_snapshot_dir()

    async def download_alphavirus_genomes(self, limit: int | None = None) -> list[GenomeData]:
        """Load genomes from ``<snapshot>/alphavirus_genomes.tsv``.

        The snapshot TSV has only 2 columns (genome_id + genome_name).
        The ``GenomeData`` dataclass has 6 fields; we populate what we
        have and leave ``genome_length`` = 0, ``taxon_lineage`` = "",
        ``genome_status`` = "snapshot", ``contigs`` = 0. Downstream
        code that reads these fields (size filter, lineage match)
        should tolerate the defaults; the size filter override below
        also adapts.
        """
        path = self.snapshot_dir / GENOMES_TSV_BASENAME
        rows = _read_tsv(path)
        genomes = [
            GenomeData(
                genome_id=r["genome.genome_id"],
                genome_length=int(r.get("genome.genome_length", 0) or 0),
                genome_name=r["genome.genome_name"],
                taxon_lineage=r.get("genome.taxon_lineage", ""),
                genome_status=r.get("genome.genome_status", "snapshot"),
                contigs=int(r.get("genome.contigs", 0) or 0),
            )
            for r in rows
        ]
        if limit is not None:
            genomes = genomes[:limit]
        log.info(
            "BVBRCSnapshotTool: loaded %d genomes from %s",
            len(genomes),
            path.name,
        )
        return genomes

    async def filter_genomes_by_size(self, genomes: list[GenomeData]) -> list[GenomeData]:
        """Snapshot-variant pass-through.

        The 2-column snapshot TSV has no ``genome.genome_length`` field,
        so ``GenomeData.genome_length`` is 0 for every row loaded from
        it. Running the base class's range-filter against 0s would
        drop everything. We pass-through instead and log once per
        call. If the caller wants real size filtering, regenerate the
        snapshot with the genome_length column.
        """
        unfiltered_count = sum(1 for g in genomes if g.genome_length == 0)
        if unfiltered_count:
            log.info(
                "BVBRCSnapshotTool.filter_genomes_by_size: %d/%d genomes had "
                "genome_length=0 (snapshot lacks the column); passing all "
                "%d through unfiltered.",
                unfiltered_count,
                len(genomes),
                len(genomes),
            )
            return list(genomes)
        # If the snapshot somehow carries genome_length, defer to base.
        return await super().filter_genomes_by_size(genomes)

    async def get_unique_protein_md5s(self, genome_ids: list[str]) -> list[ProteinData]:
        """Load protein rows from ``<snapshot>/alphavirus_proteins.tsv``
        filtered to the requested genome_ids. Returns ``ProteinData``
        entries without sequences (call ``get_feature_sequences`` next
        to populate).
        """
        path = self.snapshot_dir / PROTEINS_TSV_BASENAME
        rows = _read_tsv(path)
        genome_id_set = set(genome_ids)
        # Dedupe by md5 while preserving the first-seen entry per md5.
        seen_md5: set[str] = set()
        out: list[ProteinData] = []
        for r in rows:
            if r["genome.genome_id"] not in genome_id_set:
                continue
            md5 = r["feature.aa_sequence_md5"]
            if md5 in seen_md5:
                continue
            seen_md5.add(md5)
            out.append(
                ProteinData(
                    aa_sequence_md5=md5,
                    patric_id=r.get("feature.patric_id", ""),
                    product=r.get("feature.product", ""),
                    aa_sequence="",  # populated by get_feature_sequences
                    genome_id=r.get("feature.genome_id", r["genome.genome_id"]),
                )
            )
        log.info(
            "BVBRCSnapshotTool: loaded %d unique proteins across %d genomes",
            len(out),
            len(genome_id_set),
        )
        return out

    async def get_feature_sequences(self, md5_list: list[str]) -> list[ProteinData]:
        """Populate aa_sequences by reading the annotated FASTA.

        Returns ``ProteinData`` entries with sequence populated.
        Entries whose md5 is missing from the FASTA are skipped and
        logged — callers get a shorter list than they asked for, but
        that's an honest signal that the snapshot is incomplete.
        """
        path = self.snapshot_dir / PROTEINS_FASTA_BASENAME
        sequences = _read_fasta_by_md5(path)
        out: list[ProteinData] = []
        missing = 0
        for md5 in md5_list:
            seq = sequences.get(md5)
            if seq is None:
                missing += 1
                continue
            out.append(
                ProteinData(
                    aa_sequence_md5=md5,
                    patric_id="",
                    product="",
                    aa_sequence=seq,
                    genome_id="",
                )
            )
        if missing:
            log.warning(
                "BVBRCSnapshotTool.get_feature_sequences: %d/%d md5s missing " "from %s",
                missing,
                len(md5_list),
                path.name,
            )
        return out

    # download_alphavirus_genomes / filter_genomes_by_size /
    # get_unique_protein_md5s / get_feature_sequences are the only
    # live-API paths we hit. create_annotated_fasta is a pure
    # transform in the base class (takes ProteinData list, returns
    # FASTA string) — inheriting unchanged.

    async def execute_command(  # type: ignore[override]
        self, command: list[str], **kwargs: Any
    ):
        """Block any attempt to shell out to the BV-BRC CLI from a
        snapshot run. A snapshot deployment means no BV-BRC tooling
        is guaranteed installed locally; letting the base class try
        to ``p3-all-genomes`` against a non-existent binary produces
        cryptic errors. Raise immediately with a clear message.
        """
        raise RuntimeError(
            "BVBRCSnapshotTool is snapshot-only — ``execute_command`` is "
            "disabled. If you need live BV-BRC CLI output, use the base "
            "``BVBRCTool`` directly with a proper tool install."
        )
