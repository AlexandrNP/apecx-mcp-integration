"""Parse NCBI Taxonomy dump files for the synonym dictionary build.

Four parsers, one per NCBI taxdump file:

- :func:`parse_nodes_dmp` — (child_taxon_id, parent_taxon_id) pairs.
- :func:`parse_merged_dmp` — (old_taxon_id, new_taxon_id) pairs.
- :func:`parse_names_dmp` — every name record (all NCBI name classes).
  **Added 2026-06-08 (SC-A3)** to bring all 7 NCBI name classes into
  the dictionary; the build previously sourced synonyms only from
  per-IRI OLS lookups, leaving the dictionary bounded by the corpus's
  resolved IRI set.
- :func:`parse_delnodes_dmp` — deleted taxon ids.
  **Added 2026-06-08 (SC-A3)** so a user pasting a stale IRI sees a
  loud ``"taxon deleted"`` result instead of a silent miss.

Plus one in-memory helper:

- :func:`compute_subtree_descendants` — BFS over ``nodes.dmp`` from a
  given root taxon (e.g., ``10239`` for Viruses) to collect every
  descendant taxon id. Used by the build to scope synthetic NCBI
  entries to the virus subtree per Q1 of
  ``apecx-harvesters-work/design/SYNONYM_COMPLETENESS_PLAN.md``.

All four dump files ship inside the ``taxdump.tar.gz`` archive at
``https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz``.

Field separator in every file is ``\\t|\\t`` (tab-pipe-tab).
Each line ends with ``\\t|\\n`` so the last field after splitting on
``\\t|\\t`` carries a trailing ``\\t|``. We strip it where it matters
(merged.dmp's second column, names.dmp's fourth column).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# NCBI Taxonomy root id for the Viruses superkingdom. Used as the default
# subtree root for the synonym-completeness build (Q1 = virus subtree).
NCBITAXON_VIRUSES_ROOT = 10239


# The 7 NCBI name classes the synonym-completeness plan commits to ingest
# (SC-A4 / D10). Names.dmp ships additional classes (``includes``,
# ``in-part``, ``type material``, ``authority``, ``misspelling``, etc.)
# which are deliberately excluded as low signal / high noise.
NCBI_NAME_CLASSES_INGESTED: frozenset[str] = frozenset(
    {
        "scientific name",
        "synonym",
        "equivalent name",
        "common name",
        "genbank common name",
        "acronym",
        "blast name",
    }
)


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


@dataclass(frozen=True)
class NameRecord:
    """One row of ``names.dmp``.

    Attributes
    ----------
    taxon_id:
        The taxon id this name applies to.
    name_text:
        The surface form (e.g. ``"Eastern equine encephalitis virus"``,
        ``"EEEV"``, ``"Eastern equine encephalomyelitis virus"``).
    unique_name:
        NCBI's disambiguator string when ``name_text`` is reused across
        taxa (e.g. ``"Bacillus <bacterium>"``). Empty string for the
        common case.
    name_class:
        NCBI's classification of the name's role. One of (non-exhaustive):
        ``scientific name``, ``synonym``, ``equivalent name``,
        ``common name``, ``genbank common name``, ``acronym``,
        ``blast name``, ``includes``, ``in-part``, ``type material``,
        ``authority``, ``misspelling``. The synonym-completeness build
        filters by :data:`NCBI_NAME_CLASSES_INGESTED`.
    """

    taxon_id: int
    name_text: str
    unique_name: str
    name_class: str


def parse_names_dmp(path: Path) -> Iterator[NameRecord]:
    """Yield :class:`NameRecord` for every line of ``names.dmp``.

    names.dmp columns (tab-pipe-tab separated):
      0: tax_id
      1: name_txt
      2: unique_name (may be empty)
      3: name_class

    Empty / malformed lines are silently skipped. The caller decides which
    ``name_class`` values to keep (see :data:`NCBI_NAME_CLASSES_INGESTED`).
    """
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t|\t")
            if len(parts) < 4:
                continue
            try:
                tax_id = int(parts[0].strip())
            except ValueError:
                continue
            name_text = parts[1].strip()
            unique_name = parts[2].strip()
            # The 4th column carries the trailing ``\t|`` from the row
            # terminator; strip it before consuming.
            name_class = parts[3].rstrip("\t|\n\r").strip()
            if not name_text or not name_class:
                continue
            yield NameRecord(
                taxon_id=tax_id,
                name_text=name_text,
                unique_name=unique_name,
                name_class=name_class,
            )


def parse_delnodes_dmp(path: Path) -> Iterator[int]:
    """Yield deleted taxon ids from ``delnodes.dmp``.

    delnodes.dmp has one column (tax_id) per line:
      ``<tax_id>\\t|\\n``

    A taxon id appearing here was removed from NCBI Taxonomy; a user
    pasting an IRI referring to one should see ``resolution_status =
    UNRESOLVED`` with ``evidence = "taxon deleted"`` instead of a silent
    miss. (Wiring into the lookup pipeline is SC-A5.)
    """
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            stripped = line.rstrip("\t|\n\r").strip()
            if not stripped:
                continue
            try:
                yield int(stripped)
            except ValueError:
                continue


def compute_subtree_descendants(
    nodes_dmp_path: Path,
    root_taxon_id: int,
) -> set[int]:
    """Return the set of taxon ids in the subtree rooted at
    ``root_taxon_id`` (inclusive of the root).

    Builds a child-list adjacency map from ``nodes.dmp`` once, then BFS
    from the root. NCBI Taxonomy has ~2.7M taxa total but the virus
    subtree (root ``10239``) is ~10k — well within memory.

    The root node has ``parent_tax_id == tax_id`` per NCBI's convention;
    we suppress the self-edge to avoid an infinite loop at the root.
    """
    children: dict[int, list[int]] = {}
    for child_id, parent_id in parse_nodes_dmp(nodes_dmp_path):
        if child_id == parent_id:
            # NCBI's root self-edge — do not add ``1`` as its own child.
            continue
        children.setdefault(parent_id, []).append(child_id)

    descendants: set[int] = {root_taxon_id}
    queue: deque[int] = deque([root_taxon_id])
    while queue:
        node = queue.popleft()
        for child in children.get(node, ()):
            if child in descendants:
                continue
            descendants.add(child)
            queue.append(child)
    return descendants


def compute_subtree_descendants_union(
    nodes_dmp_path: Path,
    root_taxon_ids: list[int],
) -> set[int]:
    """Union of the subtrees rooted at each id in ``root_taxon_ids``.

    Parses ``nodes.dmp`` ONCE (the 215 MB file is expensive to read), builds
    the child-adjacency map once, then BFS from every root into a shared set.
    Used by the multi-clade dictionary expansion (viruses + bacteria + fungi
    + protozoan/helminth parasites + host taxa) so non-virus pathogens and
    host organisms resolve.
    """
    children: dict[int, list[int]] = {}
    for child_id, parent_id in parse_nodes_dmp(nodes_dmp_path):
        if child_id == parent_id:
            continue
        children.setdefault(parent_id, []).append(child_id)

    descendants: set[int] = set()
    queue: deque[int] = deque()
    for root in root_taxon_ids:
        if root not in descendants:
            descendants.add(root)
            queue.append(root)
    while queue:
        node = queue.popleft()
        for child in children.get(node, ()):
            if child in descendants:
                continue
            descendants.add(child)
            queue.append(child)
    return descendants
