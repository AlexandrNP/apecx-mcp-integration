"""SC-B4 (2026-06-08) — apply a corpus-mined synonyms JSONL sidecar to a dictionary.

Reads ``mined_observations.jsonl`` (produced by the harvesters'
SC-B2/B5 mining pipeline) and applies it to an existing dictionary
SQLite via :func:`apecx_integration.synonym_dictionary.mined_ingest.ingest_mined_observations`.

The sidecar JSONL is the contract between the apecx-harvesters mining
pipeline and the dictionary ingest. Each line is one unique
``(surface_form, taxon_id)`` pair with provenance::

    {"surface_form": "EEEV",
     "surface_form_normalized": "eeev",
     "taxon_id": 11021,
     "source": "violin_pathogen",
     "source_count": 1}

Usage:

    .venv/bin/python scripts/apply_mined_observations.py \\
        --dict-path ~/.apecx/dictionary/dictionary.sqlite \\
        --mined path/to/mined_observations.jsonl \\
        --backup
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary.mined_ingest import (  # noqa: E402
    ingest_mined_observations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply_mined_observations")
    parser.add_argument(
        "--dict-path",
        type=Path,
        default=Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite",
    )
    parser.add_argument("--mined", type=Path, required=True)
    parser.add_argument(
        "--entity-type",
        default="pathogen",
        help="Entity type to scope ingest to (default: pathogen)",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Copy dict to dictionary.sqlite.pre-mined.bak before applying.",
    )
    args = parser.parse_args(argv)

    if not args.dict_path.exists():
        print(f"ERROR: dict not found at {args.dict_path}", file=sys.stderr)
        return 1
    if not args.mined.exists():
        print(f"ERROR: mined sidecar not found at {args.mined}", file=sys.stderr)
        return 1

    if args.backup:
        backup = Path(str(args.dict_path) + ".pre-mined.bak")
        if not backup.exists():
            shutil.copy2(args.dict_path, backup)
            print(f"backup: {backup}")
        else:
            print(f"backup already exists at {backup}; not overwriting")

    summary = ingest_mined_observations(
        dict_path=args.dict_path,
        mined_jsonl=args.mined,
        entity_type=args.entity_type,
    )

    print("ingest summary:")
    for field_name, value in summary.__dict__.items():
        print(f"  {field_name:25s} {value}")

    if summary.missing_entries > 0:
        print(
            f"\nNOTE: {summary.missing_entries} mined pairs referenced taxa "
            f"that have no entry in the dictionary. These were skipped. "
            f"For virus-subtree builds this should be near zero; non-zero "
            f"means the corpus extends outside the subtree (e.g., a host "
            f"organism slipped through, or SC-A4 ingested only a subset)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
