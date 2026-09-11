"""Build the deliverable-1 term-coverage map from live PubMed + the synonym dict.

Builds the gazetteer ONCE (bounded ``LIMIT`` — never the full row count), then
harvests real PubMed abstracts for a fixed target-organism list, tags each with
the gazetteer, and MERGES the per-organism coverage into one combined list.
Writes ``docs/literature_term_coverage.json`` and prints a summary + top-15
terms by doc_count. Real-data glue script (network); not imported by tests.

    PYTHONPATH=src .venv/bin/python scripts/build_literature_coverage.py

``--db`` defaults to ``$APECX_SYNONYM_DICT_PATH`` then the workspace
``dictionary.sqlite``. ``--limit`` bounds the rows read into the gazetteer;
the default (350000) covers the target organisms in the shipped dictionary's
row order (Chikungunya sits ~275k).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.agents.literature.coverage import coverage_to_json, harvest_and_build
from apecx_integration.agents.literature.gazetteer import build_from_dictionary

_TARGET_ORGANISMS = (
    "Chikungunya virus",
    "Dengue virus",
    "Eastern equine encephalitis virus",
    "Rift Valley fever virus",
    "SARS-CoV-2",
    "Influenza A virus",
)

_DEFAULT_DB = "/Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite"
_DEFAULT_LIMIT = 350000
_DEFAULT_OUT = Path(__file__).resolve().parents[1] / "docs" / "literature_term_coverage.json"


def _merge(per_term: dict[tuple[str, str], dict], entries: list[dict]) -> None:
    """Fold one organism's coverage entries into the combined ``(term, iri)`` map.

    A term may surface for several organisms; sum ``doc_count`` and union
    ``example_pmids`` (capped at 3) so each ``(term, iri)`` is one merged entry.
    """
    for entry in entries:
        key = (entry["term"], entry["iri"])
        merged = per_term.setdefault(
            key, {"term": entry["term"], "iri": entry["iri"], "doc_count": 0, "example_pmids": []}
        )
        merged["doc_count"] += entry["doc_count"]
        for pmid in entry["example_pmids"]:
            if len(merged["example_pmids"]) < 3 and pmid not in merged["example_pmids"]:
                merged["example_pmids"].append(pmid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_literature_coverage")
    parser.add_argument("--db", default=os.environ.get("APECX_SYNONYM_DICT_PATH") or _DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--max-papers", type=int, default=20)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args(argv)

    print(f"Building gazetteer from {args.db} (limit={args.limit}) ...", flush=True)
    gazetteer = build_from_dictionary(args.db, limit=args.limit)

    per_term: dict[tuple[str, str], dict] = {}
    for organism in _TARGET_ORGANISMS:
        entries = harvest_and_build(organism, gazetteer, max_papers=args.max_papers)
        docs = sum(e["doc_count"] for e in entries)
        print(
            f"  {organism:40s} -> {len(entries):4d} terms, {docs:5d} tagged-doc mentions",
            flush=True,
        )
        _merge(per_term, entries)

    coverage = sorted(per_term.values(), key=lambda e: (-e["doc_count"], e["term"]))

    args.out.write_text(coverage_to_json(coverage))

    print()
    print(f"Organisms queried:            {len(_TARGET_ORGANISMS)}")
    print(f"Distinct (term, iri) entries: {len(coverage)}")
    print(f"Coverage JSON written:        {args.out} ({args.out.stat().st_size} bytes)")
    print()
    print("TOP 15 TERMS BY doc_count")
    print(f"{'term':32s} {'iri-tail':16s} {'doc_count':>9s}")
    for entry in coverage[:15]:
        iri_tail = entry["iri"].rsplit("/", 1)[-1].rsplit("_", 1)[-1]
        print(f"{entry['term'][:32]:32s} {iri_tail[:16]:16s} {entry['doc_count']:9d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
