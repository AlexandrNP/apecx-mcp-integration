"""apecx-fetch-taxdump — download NCBI taxdump and extract nodes.dmp + merged.dmp.

Usage::

    apecx-fetch-taxdump --output ~/.cache/apecx/taxdump

After this runs, pass the output directory to ``apecx-build-dictionary``::

    apecx-build-dictionary \\
        --violin-pathogens data/violin/Pathogen_Information.csv \\
        --output build/dictionary \\
        --ncbitaxon-nodes ~/.cache/apecx/taxdump/nodes.dmp \\
        --ncbitaxon-merged ~/.cache/apecx/taxdump/merged.dmp

The download is ~72 MB compressed; extraction produces ~270 MB.  Both are
idempotent — re-running skips the download if the files already exist.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apecx-fetch-taxdump",
        description=(
            "Download the NCBI Taxonomy dump (taxdump.tar.gz) and extract "
            "nodes.dmp + merged.dmp to a local cache directory."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory where nodes.dmp and merged.dmp will be written.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Override the download URL (default: NCBI FTP mirror).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-download and re-extract even if files already exist.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from apecx_integration.synonym_dictionary.taxdump_fetcher import (
        TAXDUMP_URL,
        fetch_taxdump,
    )

    url = args.url or TAXDUMP_URL

    try:
        nodes, merged = fetch_taxdump(
            args.output,
            url=url,
            force=args.force,
            show_progress=True,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"nodes.dmp  → {nodes}  ({nodes.stat().st_size // (1024*1024)} MiB)")
    print(f"merged.dmp → {merged}  ({merged.stat().st_size // 1024} KiB)")
    print()
    print("Next step:")
    print(
        f"  apecx-build-dictionary \\\n"
        f"      --violin-pathogens data/violin/Pathogen_Information.csv \\\n"
        f"      --output build/dictionary \\\n"
        f"      --ncbitaxon-nodes {nodes} \\\n"
        f"      --ncbitaxon-merged {merged}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
