"""SC-B end-to-end demo (2026-06-08) — mine surface→taxon pairs from local
enriched CSVs and emit ``mined_observations.jsonl``.

This is the CSV-direct equivalent of the harvesters' Globus-fed
``harmonize_index(..., mining_accumulator=acc)`` path. It exists so
SC-B can be demonstrated end-to-end against local data without Globus
credentials.

Sources mined:
- ``violin_pathogens_enriched.csv``: ``(Pathogen, NCBI_Taxonomy_ID)``
- ``bvbrc_genomes_enriched.csv``: ``(Genome Name, NCBI Taxon ID)`` and,
  when present and not strain-like, ``(Other Names, NCBI Taxon ID)``

The harvesters' canonical mining accumulator is invoked so the SC-B5
strain filter automatically drops `Influenza A virus
(A/.../H5N1)`-style strain isolates.

Usage:

    .venv/bin/python scripts/mine_corpus_csv.py \\
        --violin-pathogens ~/.apecx/dictionary/enriched/violin_pathogens_enriched.csv \\
        --bvbrc-genomes ~/.apecx/dictionary/enriched/bvbrc_genomes_enriched.csv \\
        --out mined_observations.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

# Import the harvesters accumulator from the sibling repo. The
# accumulator carries SC-B5 strain filtering, normalization, and dedup
# semantics — re-using it here keeps both the Globus-fed and
# CSV-fed paths semantically identical.
_HARVESTERS_SRC = Path(__file__).resolve().parents[2] / "apecx-harvesters-work" / "src"
if _HARVESTERS_SRC.exists():
    sys.path.insert(0, str(_HARVESTERS_SRC))

from apecx_harvesters.pipeline.corpus_mining import (  # noqa: E402
    MinedSynonymAccumulator,
)


def _coerce_taxon(raw: str) -> int | None:
    """CSV taxon columns often carry pandas-formatted floats (``"10298.0"``).
    Coerce defensively."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(float(raw)) if raw else None
    except ValueError:
        return None


def mine_violin_pathogens(path: Path, acc: MinedSynonymAccumulator) -> int:
    """Accept (Pathogen, NCBI_Taxonomy_ID) pairs. Returns rows accepted."""
    accepted = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            tid = _coerce_taxon(row.get("NCBI_Taxonomy_ID", ""))
            surface = row.get("Pathogen") or ""
            if acc.observe(surface, tid, source="violin_pathogen"):
                accepted += 1
    return accepted


def _iter_bvbrc_rows(path: Path):
    """Yield dict rows from the BVBRC enriched CSV.

    The format is non-standard: the outer CSV has one column whose
    name is the concatenated BVBRC header (all 80+ columns separated
    by commas, as one quoted string) and whose value per row is the
    original BVBRC row (also as a single quoted string with doubled-
    quote escaping). We split this nested CSV by re-parsing the inner
    column-list and the inner row with stdlib csv.
    """

    with path.open(newline="") as fh:
        outer_reader = csv.DictReader(fh)
        # The column whose name carries the inner BVBRC header.
        bvbrc_header_col = next(
            (c for c in outer_reader.fieldnames if "Genome ID" in c),
            None,
        )
        if bvbrc_header_col is None:
            return
        # Re-parse the column-list into inner field names.
        inner_fields = next(csv.reader([bvbrc_header_col]))
        for outer_row in outer_reader:
            inner_csv = outer_row.get(bvbrc_header_col, "")
            if not inner_csv:
                continue
            inner_values = next(csv.reader([inner_csv]), None)
            if not inner_values:
                continue
            # Pad / truncate to align with the field list.
            inner_dict = {
                name: (inner_values[i] if i < len(inner_values) else "")
                for i, name in enumerate(inner_fields)
            }
            # Carry through the enrichment columns from the outer row.
            for k, v in outer_row.items():
                if k != bvbrc_header_col:
                    inner_dict[k] = v
            yield inner_dict


def mine_bvbrc_genomes(path: Path, acc: MinedSynonymAccumulator) -> int:
    """Accept (Genome Name, NCBI Taxon ID) and (Other Names, ...) pairs.

    BVBRC's CSV column names use spaces (``"NCBI Taxon ID"``), not
    snake_case like the Globus index docs.
    """
    accepted = 0
    for row in _iter_bvbrc_rows(path):
        tid = _coerce_taxon(row.get("NCBI Taxon ID", ""))
        for surface_col in ("Genome Name", "Other Names"):
            surface = row.get(surface_col) or ""
            if not surface:
                continue
            # Other Names may be a semicolon-separated list.
            for piece in surface.split(";"):
                piece = piece.strip()
                if not piece:
                    continue
                if acc.observe(piece, tid, source="bvbrc_genome"):
                    accepted += 1
    return accepted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mine_corpus_csv")
    parser.add_argument(
        "--violin-pathogens",
        type=Path,
        default=Path.home()
        / ".apecx"
        / "dictionary"
        / "enriched"
        / "violin_pathogens_enriched.csv",
    )
    parser.add_argument(
        "--bvbrc-genomes",
        type=Path,
        default=Path.home() / ".apecx" / "dictionary" / "enriched" / "bvbrc_genomes_enriched.csv",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    acc = MinedSynonymAccumulator()
    if args.violin_pathogens.exists():
        n = mine_violin_pathogens(args.violin_pathogens, acc)
        print(f"violin_pathogens: {n} observations accepted")
    if args.bvbrc_genomes.exists():
        n = mine_bvbrc_genomes(args.bvbrc_genomes, acc)
        print(f"bvbrc_genomes:    {n} observations accepted")

    # Emit one JSONL row per unique (surface, taxon) pair, with the
    # full source set rolled up so the ingest can score corroboration.
    pairs_written = 0
    with args.out.open("w") as fh:
        for obs, source_count, source_set in acc.unique_pairs():
            # Emit ONE row per source for that pair so the ingest can
            # surface per-source provenance to mined_conflicts.
            for source in sorted(source_set):
                fh.write(
                    json.dumps(
                        {
                            "surface_form": obs.surface_form,
                            "surface_form_normalized": obs.surface_form_normalized,
                            "taxon_id": obs.taxon_id,
                            "source": source,
                            "source_count": source_count,
                        }
                    )
                    + "\n"
                )
                pairs_written += 1

    print(f"\nwrote {pairs_written} JSONL rows to {args.out}")
    print(f"unique (surface, taxon) pairs: {acc.unique_pair_count()}")
    print(f"surface-form conflicts: {acc.conflict_count()}")
    print("per-source stats:")
    for source, stats in sorted(acc.per_source_stats().items()):
        print(
            f"  {source:20s} observed={stats['observed']:>6d} "
            f"rejected={stats['rejected']:>6d} "
            f"unique_pairs={stats['unique_pairs']:>6d}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
