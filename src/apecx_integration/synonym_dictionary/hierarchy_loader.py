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


def parse_nodes_dmp_with_rank(path: Path) -> Iterator[tuple[int, int, str]]:
    """Yield (taxon_id, parent_taxon_id, rank) from ``nodes.dmp``.

    Same parse as :func:`parse_nodes_dmp` but also surfaces the rank
    column (``species`` / ``strain`` / ``genus`` / ``no rank`` / …),
    which the strain→species normalization needs.
    """
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.split("\t|\t")
            if len(parts) < 3:
                continue
            try:
                taxon_id = int(parts[0].strip())
                parent_id = int(parts[1].strip())
            except ValueError:
                continue
            # In the real nodes.dmp the rank is a middle column (followed by
            # more ``\t|\t``-separated fields), so it parses clean. But on a
            # minimal/truncated line where rank is the LAST field, it carries
            # the row terminator ``\t|``; strip it so rank comparisons hold.
            rank = parts[2].rstrip("\t|\n\r").strip()
            yield taxon_id, parent_id, rank


def compute_species_ancestors(
    nodes_dmp_path: Path,
    *,
    species_rank: str = "species",
) -> dict[int, int]:
    """Map every taxon at-or-below species rank to its species ancestor.

    Walks ``nodes.dmp`` once to build (child→parent) edges + (taxon→rank),
    then for each taxon walks up to the first ancestor with rank
    ``species_rank`` (a species maps to itself; a strain/subspecies maps to
    its enclosing species). Taxa ABOVE species rank (genus, family, …) have
    no species ancestor and are omitted from the result.

    Memoized so the full ~2.8M-taxon tree resolves in one near-linear pass.
    This is the build-time heavy lift that lets the runtime be a single
    table read.
    """
    parent: dict[int, int] = {}
    rank: dict[int, str] = {}
    for taxon_id, parent_id, taxon_rank in parse_nodes_dmp_with_rank(nodes_dmp_path):
        parent[taxon_id] = parent_id
        rank[taxon_id] = taxon_rank

    species_of: dict[int, int | None] = {}

    def _species_ancestor(start: int) -> int | None:
        chain: list[int] = []
        cur: int | None = start
        result: int | None = None
        while cur is not None:
            if cur in species_of:
                result = species_of[cur]
                break
            if rank.get(cur) == species_rank:
                result = cur
                break
            chain.append(cur)
            nxt = parent.get(cur)
            if nxt is None or nxt == cur:  # root / self-loop
                result = None
                break
            cur = nxt
        for node in chain:
            species_of[node] = result
        return result

    out: dict[int, int] = {}
    for taxon_id in parent:
        species = _species_ancestor(taxon_id)
        if species is not None:
            out[taxon_id] = species
    return out


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
