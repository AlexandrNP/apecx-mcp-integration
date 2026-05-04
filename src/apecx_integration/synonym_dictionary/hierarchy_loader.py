"""Parse NCBI Taxonomy dump files for the taxon-hierarchy table.

Produces (child_taxon_id, parent_taxon_id) pairs from ``nodes.dmp``
and (old_taxon_id, new_taxon_id) pairs from ``merged.dmp``.

Both files ship inside the ``taxdump.tar.gz`` archive available at
``https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz``.

Field separator in both files is ``\\t|\\t`` (tab-pipe-tab).
Each line ends with ``\\t|\\n`` so the last field after splitting on
``\\t|\\t`` carries a trailing ``\\t|``.  We strip it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)


def parse_nodes_dmp(path: Path) -> Iterator[tuple[int, int]]:
    """Yield (child_taxon_id, parent_taxon_id) from ``nodes.dmp``.

    nodes.dmp columns (tab-pipe-tab separated):
      0: tax_id
      1: parent tax_id
      2: rank
      ... (ignored)

    The root node has tax_id==parent_tax_id==1; callers must handle
    the self-loop to avoid infinite ancestor traversal.
    """
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t|\t")
            if len(parts) < 2:
                continue
            try:
                child_id = int(parts[0].strip())
                parent_id = int(parts[1].strip())
            except ValueError:
                continue
            yield child_id, parent_id


def parse_merged_dmp(path: Path) -> Iterator[tuple[int, int]]:
    """Yield (old_taxon_id, new_taxon_id) from ``merged.dmp``.

    merged.dmp columns (tab-pipe-tab separated):
      0: old_tax_id
      1: new_tax_id
    """
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t|\t")
            if len(parts) < 2:
                continue
            try:
                old_id = int(parts[0].strip())
                new_id = int(parts[1].strip().rstrip("\t|"))
            except ValueError:
                continue
            yield old_id, new_id


def count_lines(path: Path) -> int:
    """Return approximate row count for progress logging (reads the file once)."""
    count = 0
    with path.open(encoding="latin-1") as fh:
        for _ in fh:
            count += 1
    return count
