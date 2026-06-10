"""Local demonstration of harmonization wins (2026-06-08).

Runs comparative queries against the enriched corpus CSVs in TWO modes:

- **raw_text** — substring match on the source's natural surface fields
  (``Pathogen`` for VIOLIN, ``Genome Name`` + ``Other Names`` for
  BVBRC). Simulates what a Globus Search ``q="..."`` query gets on
  the un-harmonized indices.

- **harmonized** — resolve the user term via the synonym dictionary
  (``lookup_entity``) to one or more canonical IRIs, then match on
  the ``canonical_iri`` enrichment column. Simulates what a Globus
  Search ``subjects.valueUri:"<iri>"`` filter gets on the harmonized
  indices that include canonical_iri as a subject.

The enriched CSVs at ``~/.apecx/dictionary/enriched/`` are the same
data that would feed the harmonized Globus indices — they carry the
``canonical_iri`` / ``canonical_label`` / ``resolution_status``
enrichment columns that an ingest step would land in production.

Output: per-query table showing raw_text recall vs harmonized recall,
unique records each mode found, and overlap. Emits a Markdown report
to stdout (or `--out` path) for inclusion in the agent-skill docs.

Usage:

    .venv/bin/python scripts/demo_harmonization_wins.py
    .venv/bin/python scripts/demo_harmonization_wins.py --out /tmp/wins.md
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary import loader as _loader  # noqa: E402
from apecx_integration.synonym_dictionary.enums import EntityType  # noqa: E402
from apecx_integration.synonym_dictionary.lookup import lookup_entity  # noqa: E402


@dataclass
class CorpusRecord:
    """One harvested record's relevant fields for query matching."""

    record_id: str
    source: str
    title: str  # surface form for raw match
    surface_aliases: tuple[str, ...]  # additional raw fields
    canonical_iri: str | None
    canonical_label: str | None
    raw_blob: str  # concatenated free-text for raw substring match


def _load_merged_taxons() -> dict[int, int]:
    """Read NCBI's old→new taxon merge map from the dictionary SQLite.

    Used by the demonstrator to fix the harmonization-meets-rename
    gap: when BVBRC records carry a taxon id NCBI has since merged
    into a new id, the dictionary lookup hits the NEW id while the
    record still tags the OLD id — filter equality misses every
    such record. Walking ``merged_taxons`` closes the gap.
    """
    import sqlite3

    dict_path = Path(
        os.environ.get(
            "APECX_SYNONYM_DICT_PATH",
            str(Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"),
        )
    )
    if not dict_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{dict_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT old_taxon_id, new_taxon_id FROM merged_taxons").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return {}
    return {int(old): int(new) for old, new in rows}


def _iter_bvbrc_rows(path: Path) -> Iterable[dict]:
    """Parse BVBRC's nested CSV (outer field carries inner CSV)."""
    with path.open(newline="") as fh:
        outer = csv.DictReader(fh)
        header_col = next((c for c in outer.fieldnames if "Genome ID" in c), None)
        if header_col is None:
            return
        inner_fields = next(csv.reader([header_col]))
        for outer_row in outer:
            inner_csv = outer_row.get(header_col, "")
            if not inner_csv:
                continue
            values = next(csv.reader([inner_csv]), None)
            if not values:
                continue
            d = {
                name: (values[i] if i < len(values) else "") for i, name in enumerate(inner_fields)
            }
            for k, v in outer_row.items():
                if k != header_col:
                    d[k] = v
            yield d


def load_corpus(base_dir: Path) -> list[CorpusRecord]:
    """Load violin_pathogens + bvbrc_genomes enriched CSVs."""
    records: list[CorpusRecord] = []

    # VIOLIN pathogens
    vp = base_dir / "violin_pathogens_enriched.csv"
    if vp.exists():
        df = pd.read_csv(vp)
        for idx, row in df.iterrows():
            pathogen = str(row.get("Pathogen") or "")
            disease = str(row.get("Disease") or "")
            desc = str(row.get("Pathogen_Description") or "")
            aliases = tuple(s for s in (disease,) if s and s != "nan")
            records.append(
                CorpusRecord(
                    record_id=f"violin_pathogen:{row.get('id', idx)}",
                    source="violin_pathogen",
                    title=pathogen,
                    surface_aliases=aliases,
                    canonical_iri=(
                        row["canonical_iri"] if pd.notna(row.get("canonical_iri")) else None
                    ),
                    canonical_label=(
                        row["canonical_label"] if pd.notna(row.get("canonical_label")) else None
                    ),
                    raw_blob=" ".join(
                        s for s in (pathogen, disease, desc) if s and s != "nan"
                    ).lower(),
                )
            )

    # BVBRC genomes — apply on-the-fly harmonization since the
    # enriched CSV's canonical_iri column is empty for BVBRC (the
    # 2026-05-12 build pass didn't write back). Simulate what a
    # production harvester ingest would do: derive the species-level
    # canonical IRI from BVBRC's ``NCBI Taxon ID`` field (which uses
    # ``species.strain`` convention — first dot-component is species).
    # ALSO walk the merged_taxons table to resolve old/renamed taxa
    # to NCBI's current canonical (e.g., Hepacivirus C 11103 →
    # Orthohepacivirus hominis 3052230). Without this, records still
    # tagged with the old species id miss the harmonized filter
    # entirely — exactly the gap harmonization is supposed to close.
    merged_map = _load_merged_taxons()
    bv = base_dir / "bvbrc_genomes_enriched.csv"
    if bv.exists():
        _NCBI = "http://purl.obolibrary.org/obo/NCBITaxon_"
        for row in _iter_bvbrc_rows(bv):
            name = (row.get("Genome Name") or "").strip()
            other = (row.get("Other Names") or "").strip()
            strain = (row.get("Strain") or "").strip()
            species = (row.get("Species") or "").strip()
            aliases = tuple(s for s in (other, strain, species) if s and s.lower() != "nan")

            # On-the-fly harmonization: extract species id from
            # ``NCBI Taxon ID`` (BVBRC's species.strain convention,
            # e.g. "11021.6497" -> species 11021).
            iri = (row.get("canonical_iri") or "").strip() or None
            label = (row.get("canonical_label") or "").strip() or None
            if not iri:
                raw_taxon = (row.get("NCBI Taxon ID") or "").strip()
                if raw_taxon:
                    species_part = raw_taxon.split(".", 1)[0]
                    try:
                        species_id = int(species_part)
                        if species_id > 0:
                            # Resolve old/merged species id to current
                            # NCBI canonical. Without this step,
                            # post-rename taxa (e.g., HCV) miss.
                            species_id = merged_map.get(species_id, species_id)
                            iri = f"{_NCBI}{species_id}"
                            label = species or name  # best-effort
                    except ValueError:
                        pass
            records.append(
                CorpusRecord(
                    record_id=f"bvbrc_genome:{row.get('Genome ID', '?')}",
                    source="bvbrc_genome",
                    title=name,
                    surface_aliases=aliases,
                    canonical_iri=iri,
                    canonical_label=label,
                    raw_blob=" ".join(s for s in (name, other, strain, species) if s).lower(),
                )
            )
    return records


def query_raw(records: list[CorpusRecord], term: str) -> list[CorpusRecord]:
    """Substring match (case-insensitive) on the concatenated free-text blob.

    Simulates the un-harmonized Globus Search ``q="EEEV"`` baseline:
    matches records whose surface fields literally contain the query
    string. **No synonym expansion, no canonical-IRI awareness.**
    """
    needle = term.lower().strip()
    if not needle:
        return []
    # Use word-boundary match so "ZIK" doesn't match "Zaire" but does
    # match "Zika virus". This is roughly what Globus Search's
    # tokenization would do.
    pattern = re.compile(r"\b" + re.escape(needle) + r"\b", re.IGNORECASE)
    return [r for r in records if pattern.search(r.raw_blob)]


def query_harmonized(
    records: list[CorpusRecord], term: str
) -> tuple[list[CorpusRecord], list[str], str]:
    """Resolve ``term`` via lookup_entity then match on canonical_iri.

    Returns ``(matching_records, candidate_iris, resolution_path)``.

    - On fast hit: candidate_iris = [the IRI]; records where
      ``canonical_iri == that IRI``.
    - On ambiguous: candidate_iris = all IRIs; matches union over all.
    - On miss / deleted / fuzzy: handled per the lookup contract.
    """
    result = lookup_entity(term, entity_type=EntityType.PATHOGEN)
    if result.path == "miss":
        return [], [], "miss"
    if result.path == "ambiguous":
        candidate_iris = [c.canonical_iri for c in result.candidates]
        matches = [r for r in records if r.canonical_iri in set(candidate_iris)]
        return matches, candidate_iris, "ambiguous"
    if result.path in ("fast", "fuzzy", "ancestor", "slow"):
        if result.canonical_iri is None:
            return [], [], result.path
        candidate_iris = [result.canonical_iri]
        matches = [r for r in records if r.canonical_iri == result.canonical_iri]
        return matches, candidate_iris, result.path
    return [], [], result.path


@dataclass
class ComparisonResult:
    query: str
    raw_count: int
    harmonized_count: int
    overlap_count: int
    raw_only_count: int
    harmonized_only_count: int
    candidate_iris: list[str]
    harmonized_path: str
    notes: str = ""
    raw_only_samples: list[str] = field(default_factory=list)
    harmonized_only_samples: list[str] = field(default_factory=list)


def compare(records: list[CorpusRecord], term: str, notes: str = "") -> ComparisonResult:
    raw_hits = {r.record_id for r in query_raw(records, term)}
    hm_recs, candidate_iris, path = query_harmonized(records, term)
    hm_hits = {r.record_id for r in hm_recs}
    overlap = raw_hits & hm_hits
    raw_only = raw_hits - hm_hits
    hm_only = hm_hits - raw_hits

    # Sample some raw_only / harmonized_only titles for illustration.
    by_id = {r.record_id: r for r in records}
    raw_only_titles = [by_id[i].title for i in list(raw_only)[:3] if i in by_id]
    hm_only_titles = [by_id[i].title for i in list(hm_only)[:3] if i in by_id]

    return ComparisonResult(
        query=term,
        raw_count=len(raw_hits),
        harmonized_count=len(hm_hits),
        overlap_count=len(overlap),
        raw_only_count=len(raw_only),
        harmonized_only_count=len(hm_only),
        candidate_iris=candidate_iris,
        harmonized_path=path,
        notes=notes,
        raw_only_samples=raw_only_titles,
        harmonized_only_samples=hm_only_titles,
    )


# ---------------------------------------------------------------------------
# Demo queries — hand-curated to exercise different harmonization wins.
# ---------------------------------------------------------------------------

DEMO_QUERIES: list[tuple[str, str]] = [
    # ------------------------------------------------------------------
    # Acronym wins — the corpus has many records with the species name
    # but never the acronym verbatim; harmonization catches them all.
    # ------------------------------------------------------------------
    (
        "EEEV",
        "Acronym → species. Raw matches genome names with 'EEEV' "
        "substring (~456 records). Harmonization resolves to "
        "NCBITaxon_11021 and catches ALL Eastern equine encephalitis "
        "virus records (~1426) — a 3× recall lift.",
    ),
    (
        "VEEV",
        "Acronym → Venezuelan equine encephalitis virus (NCBITaxon_11036). "
        "Many BVBRC records use only the verbose species name; "
        "harmonization recovers them.",
    ),
    (
        "WEEV",
        "Acronym → Western equine encephalitis virus (NCBITaxon_11039).",
    ),
    (
        "MAYV",
        "Acronym → Mayaro virus (NCBITaxon_59301). 239 records in "
        "BVBRC carry the species name; raw substring match for 'MAYV' "
        "may or may not catch them depending on whether the genome "
        "name format includes the acronym.",
    ),
    # ------------------------------------------------------------------
    # NCBI rename wins — taxon got a new canonical name; raw text "
    # matching on the old name misses the new-name records.
    # ------------------------------------------------------------------
    (
        "Rift Valley fever virus",
        "NCBI rename → ICTV now lists 'Phlebovirus riftense'. "
        "Harmonization via NCBITaxon_11588 catches records labeled "
        "with EITHER name (1811 records in BVBRC).",
    ),
    # ------------------------------------------------------------------
    # Same-name baselines — both modes should match similarly.
    # ------------------------------------------------------------------
    (
        "Chikungunya virus",
        "Verbose species name. Both raw and harmonized should hit "
        "~8413 BVBRC records; harmonized via NCBITaxon_37124.",
    ),
    (
        "Sindbis virus",
        "Verbose species name. Both modes should match similarly (~801 BVBRC records).",
    ),
    # ------------------------------------------------------------------
    # AMBIGUOUS routing — surfaces choice instead of silent pick.
    # ------------------------------------------------------------------
    (
        "RSV",
        "AMBIGUOUS — dictionary surfaces 6 candidate taxa "
        "(Human/Bovine/Ovine orthopneumovirus + Rous sarcoma + "
        "Tenuivirus + clade). Raw substring match silently lumps them "
        "all. Win is QUALITATIVE (HITL prompt), not quantitative.",
    ),
    (
        "adenovirus",
        "AMBIGUOUS — Adenoviridae (family) + 'unidentified adenovirus' "
        "(placeholder). SC-B mining surfaced the ambiguity; pre-SC-B "
        "the dictionary silently picked one.",
    ),
    # ------------------------------------------------------------------
    # Brutal-truth gaps — harmonization does NOT fix these.
    # ------------------------------------------------------------------
    (
        "CHIKV",
        "**KNOWN GAP** — NCBI lacks 'CHIKV' as a curated acronym; "
        "SC-B mining didn't catch it because BVBRC's genome names "
        "embed strain detail (e.g., 'Chikungunya virus CHIKV/IRL/2007') "
        "that SC-B5 filtered as strain-level. **Raw substring catches "
        "thousands; harmonization misses them all.** A win for raw on "
        "this query — and exactly the class of gap that needs a "
        "different fix (manual curation or non-SC-B5-filtered mining).",
    ),
    (
        "alphavirus",
        "Family-level vernacular not in NCBI's name set. Both modes "
        "miss the intent (the genus is Alphavirus). Harmonization "
        "would need a vernacular-mapping pass.",
    ),
]


def render_markdown(results: list[ComparisonResult]) -> str:
    lines = [
        "# Harmonization wins — local demonstration (2026-06-08)",
        "",
        "Runs 12 query scenarios against the enriched VIOLIN+BVBRC corpus "
        "CSVs (the same data that would feed the harmonized Globus indices). "
        "Compares `raw_text` substring matching against the `harmonized` "
        "canonical-IRI filter mode.",
        "",
        "**Each row reports**:",
        "",
        "- `raw` — record count from substring matching on source surface fields",
        "- `harmonized` — record count from canonical-IRI matching after "
        "  resolving the term via the synonym dictionary",
        "- `overlap` — records both modes hit",
        "- `raw_only` — records the raw mode hit but harmonized missed "
        "  (typically false positives — substring matched something else)",
        "- `hm_only` — records harmonized hit that raw missed "
        "  (the *real win* — harmonization-recoverable records)",
        "",
        "| Query | Path | raw | harm | overlap | raw_only | hm_only | Δ recall |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        delta = r.harmonized_count - r.raw_count
        delta_sign = f"{delta:+d}" if delta != 0 else "0"
        candidate_str = (
            f"({len(r.candidate_iris)} candidates)" if r.harmonized_path == "ambiguous" else ""
        )
        lines.append(
            f"| `{r.query}` | {r.harmonized_path} {candidate_str} | "
            f"{r.raw_count} | {r.harmonized_count} | {r.overlap_count} | "
            f"{r.raw_only_count} | {r.harmonized_only_count} | {delta_sign} |"
        )

    lines.append("")
    lines.append("## Notes per query")
    lines.append("")
    for r in results:
        lines.append(f"### `{r.query}`")
        lines.append("")
        lines.append(r.notes)
        lines.append("")
        lines.append(f"- Resolution path: `{r.harmonized_path}`")
        if r.candidate_iris:
            lines.append(
                "- Candidate IRIs: "
                + ", ".join(f"`{iri.rsplit('_', 1)[-1]}`" for iri in r.candidate_iris[:6])
                + (f" (+{len(r.candidate_iris) - 6} more)" if len(r.candidate_iris) > 6 else "")
            )
        if r.harmonized_only_samples:
            lines.append(
                "- Records ONLY harmonized found (sample): "
                + "; ".join(repr(t) for t in r.harmonized_only_samples)
            )
        if r.raw_only_samples and r.raw_only_count > 0:
            lines.append(
                f"- Records ONLY raw found ({r.raw_only_count} total, "
                f"likely false positives): " + "; ".join(repr(t) for t in r.raw_only_samples)
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo_harmonization_wins")
    parser.add_argument(
        "--enriched-dir",
        type=Path,
        default=Path.home() / ".apecx" / "dictionary" / "enriched",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--queries-only",
        action="store_true",
        help="Only run a subset of the demo queries (for quick smoke).",
    )
    args = parser.parse_args(argv)

    # Configure dictionary path so lookup_entity works.
    dict_path = Path(
        os.environ.get(
            "APECX_SYNONYM_DICT_PATH",
            str(Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"),
        )
    )
    _loader._singleton.configure(dict_path)

    print(f"loading corpus from {args.enriched_dir}", file=sys.stderr)
    records = load_corpus(args.enriched_dir)
    print(
        f"loaded {len(records)} records "
        f"({sum(1 for r in records if r.source == 'violin_pathogen')} VIOLIN + "
        f"{sum(1 for r in records if r.source == 'bvbrc_genome')} BVBRC)",
        file=sys.stderr,
    )

    queries = DEMO_QUERIES[:4] if args.queries_only else DEMO_QUERIES
    results = [compare(records, term, notes) for term, notes in queries]

    md = render_markdown(results)
    if args.out:
        args.out.write_text(md)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
