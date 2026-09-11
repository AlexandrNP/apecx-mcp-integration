"""Build a taxon-stamped literature corpus from real PubMed + the synonym dict.

Builds the gazetteer ONCE (bounded ``LIMIT`` — never the full 1.3M rows), then
harvests + stamps a small fixed list of target organisms and writes every
stamped record to a JSONL. Real-data glue script; not imported by tests.

    PYTHONPATH=src .venv/bin/python scripts/build_stamped_corpus.py \\
        --db /Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite \\
        --out /tmp/stamped_corpus.jsonl

``--db`` defaults to ``$APECX_SYNONYM_DICT_PATH``. ``--limit`` bounds the rows
read into the gazetteer; the default (300000) covers the target organisms below
in the shipped dictionary's row order.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.agents.literature.gazetteer import build_from_dictionary
from apecx_integration.agents.literature.pipeline import harvest_and_stamp

_TARGET_ORGANISMS = (
    "Chikungunya virus",
    "Eastern equine encephalitis virus",
    "Rift Valley fever virus",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_stamped_corpus")
    parser.add_argument(
        "--db",
        default=os.environ.get("APECX_SYNONYM_DICT_PATH"),
        help="Path to dictionary.sqlite (default: $APECX_SYNONYM_DICT_PATH).",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output JSONL path.")
    parser.add_argument(
        "--limit",
        type=int,
        default=300_000,
        help="Bounded LIMIT of dictionary rows read into the gazetteer.",
    )
    parser.add_argument(
        "--max-papers",
        type=int,
        default=5,
        help="Max PubMed records to harvest per organism.",
    )
    args = parser.parse_args(argv)

    if not args.db:
        parser.error("no dictionary path: pass --db or set APECX_SYNONYM_DICT_PATH")

    gazetteer = build_from_dictionary(args.db, limit=args.limit)

    records: list[dict] = []
    for organism in _TARGET_ORGANISMS:
        records.extend(harvest_and_stamp(organism, gazetteer, max_papers=args.max_papers))

    with args.out.open("w") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")

    with_iri = sum(1 for r in records if r["iris"])
    print(
        f"{len(_TARGET_ORGANISMS)} organisms | {len(records)} records | "
        f"{with_iri} with >=1 iri -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
