"""Root-cause classification for 0-coverage (harm_total == 0) cells — WHY did the harmonized
``subjects.valueUri`` filter return nothing for this (pathogen × index)?

Pure: inspects the RAW-leg records (the records that DO exist in the index, if any) via the same
``_datacite`` readers the product uses, and decides among four causes. The four map onto the user's
three asks (missing source id / genuinely missing / harmonization failure) plus one honest refinement:

  - ``stamping_mismatch``   (harmonization failure): raw > 0 & harm == 0, and ≥1 raw record's SOURCE
        NCBI-Taxonomy id sits IN the queried subtree — the record IS about the organism but was never
        stamped with the canonical ``subjects.valueUri`` (stale ICTV rename / re-index gap). The filter
        SHOULD have matched it and didn't. This is the fixable-by-restamping class.
  - ``missing_source_id``   (missing source identifier): raw > 0 & harm == 0, and NO raw record carries
        any taxon id at all (no ``subjects.valueUri`` AND no ``NCBI-Taxonomy`` alt-id) — there was
        nothing to stamp (e.g. a structure record whose only organism evidence is
        ``pdb.polymer_entities[].scientific_name``). "Missing UniProt-style linkage" lives here.
  - ``offtarget_raw_match`` (genuinely missing, refined): raw > 0 & harm == 0, raw records carry taxon
        ids but NONE in the queried subtree — the raw full-text query matched records of OTHER
        organisms; this index has no record about THIS organism (the raw leg is a precision hazard, not
        coverage). Folded with ``genuinely_absent`` for the "does the index hold this organism?" view.
  - ``genuinely_absent``    (genuinely missing): raw == 0 & harm == 0 — no record at all. CAVEAT (the
        product's own ``zero_floor_unclear``): a stale-dict canonical label can masquerade as a real
        absence when the raw query term also fails to match; this class cannot fully exclude that.

``covered`` is returned for a harm > 0 cell (not a 0-coverage cell) so the classifier is total.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search._datacite import (
    datacite_identifiers,
    datacite_organisms,
    datacite_taxon_iris,
)
from tests.eval.harmonization.judges import source_taxon_ids

# The four 0-coverage causes + ``covered``; grouped for the "does the index hold this organism?" view.
MISSING_ORGANISM = ("genuinely_absent", "offtarget_raw_match")  # index has no record about it
HARMONIZATION_FIXABLE = ("stamping_mismatch",)  # record exists + is about it, just not stamped


def _has_any_taxon_id(record) -> bool:
    """True if the record carries ANY taxon stamp the harmonized filter could have used — a
    ``subjects.valueUri`` IRI or a source ``NCBI-Taxonomy`` alternateIdentifier."""
    return bool(datacite_taxon_iris(record)) or bool(
        datacite_identifiers(record).get("NCBI-Taxonomy")
    )


def classify_cell(
    raw_records: list, harm_total: int, raw_total: int, subtree_ids: set[int]
) -> dict:
    """Classify one cell. ``subtree_ids`` is the queried species' taxon subtree (from
    ``judges.build_subtree``); empty for a resolution-miss cell (no IRI), which collapses the
    in-subtree test to 0 so a miss cell is ``missing_source_id``/``offtarget``/``absent`` by its raw
    records alone — never ``stamping_mismatch`` (there was no filter to mis-stamp against)."""
    if harm_total > 0:
        return {"class": "covered", "raw_n": len(raw_records)}
    if raw_total == 0:
        return {"class": "genuinely_absent", "raw_n": 0, "in_subtree": 0, "with_taxon_id": 0}

    in_subtree = sum(1 for r in raw_records if any(t in subtree_ids for t in source_taxon_ids(r)))
    with_taxon_id = sum(1 for r in raw_records if _has_any_taxon_id(r))
    organism_only = sum(
        1 for r in raw_records if not _has_any_taxon_id(r) and datacite_organisms(r)
    )

    if in_subtree > 0:
        cls = "stamping_mismatch"
    elif with_taxon_id > 0:
        cls = "offtarget_raw_match"
    else:
        cls = "missing_source_id"
    return {
        "class": cls,
        "raw_n": len(raw_records),
        "in_subtree": in_subtree,
        "with_taxon_id": with_taxon_id,
        "organism_only": organism_only,
    }


def rootcause_matrix(cells: list[dict]) -> dict[str, dict[str, int]]:
    """Per-index tally of the root-cause classes across the 0-coverage cells (cells carrying a
    ``rootcause`` dict). ``covered`` cells are counted too so each index's column sums to its cell
    count — the coverage picture and its failure decomposition in one table."""
    matrix: dict[str, dict[str, int]] = {}
    for cell in cells:
        rc = cell.get("rootcause")
        if not rc:
            continue
        col = matrix.setdefault(cell["index"], {})
        col[rc["class"]] = col.get(rc["class"], 0) + 1
    return matrix
