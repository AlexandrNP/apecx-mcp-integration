"""One-shot delta-update: add NCBI ``includes`` rows for the virus
subtree as synonyms to an existing dictionary.

The full SC-A4 rebuild path is the right answer for a from-scratch
build, but applying one extra name class against the existing prod
dictionary is far cheaper as a surgical delta. This script:

1. Parses ``names.dmp`` for rows where ``name_class == "includes"``
   and ``tax_id`` is in the virus subtree (NCBITaxon:10239).
2. For each row, looks up the existing entry by ``NCBITaxon_<tax_id>``.
3. Appends the name to that entry's ``synonyms_json`` (dedup).
4. Inserts the normalized form into ``inverse_index``, honoring the
   existing SQLite writer's last-write-wins / ambiguity-capture
   semantics.

This does NOT change schema, manifest, or any other table. The
companion source change (``NCBI_NAME_CLASSES_INGESTED`` += "includes")
ensures the next full rebuild produces an equivalent result.

Usage:

    .venv/bin/python scripts/apply_includes_delta.py \\
        --dict-path ~/.apecx/dictionary/dictionary.sqlite \\
        --names-dmp ~/.apecx/taxdump/names.dmp \\
        --nodes-dmp ~/.apecx/taxdump/nodes.dmp
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: E402
    NCBITAXON_VIRUSES_ROOT,
    compute_subtree_descendants,
    parse_names_dmp,
)
from apecx_integration.synonym_dictionary.normalization import (  # noqa: E402
    normalize_surface_form,
)

_NCBITAXON_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply_includes_delta")
    parser.add_argument(
        "--dict-path",
        type=Path,
        default=Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite",
    )
    parser.add_argument(
        "--names-dmp",
        type=Path,
        default=Path.home() / ".apecx" / "taxdump" / "names.dmp",
    )
    parser.add_argument(
        "--nodes-dmp",
        type=Path,
        default=Path.home() / ".apecx" / "taxdump" / "nodes.dmp",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the delta but do not write to the dictionary.",
    )
    args = parser.parse_args(argv)

    for path in (args.dict_path, args.names_dmp, args.nodes_dmp):
        if not path.exists():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1

    t0 = time.monotonic()
    print(f"computing virus-subtree descendants from {args.nodes_dmp}...")
    descendants = compute_subtree_descendants(args.nodes_dmp, NCBITAXON_VIRUSES_ROOT)
    print(f"  {len(descendants)} taxa in virus subtree ({time.monotonic() - t0:.1f}s)")

    t1 = time.monotonic()
    print(f"scanning {args.names_dmp} for 'includes' rows in subtree...")
    includes_by_taxon: dict[int, list[str]] = {}
    total_includes_rows = 0
    for rec in parse_names_dmp(args.names_dmp):
        if rec.name_class != "includes":
            continue
        total_includes_rows += 1
        if rec.taxon_id not in descendants:
            continue
        includes_by_taxon.setdefault(rec.taxon_id, []).append(rec.name_text)
    print(
        f"  {total_includes_rows} total 'includes' rows in dump; "
        f"{sum(len(v) for v in includes_by_taxon.values())} in virus subtree "
        f"across {len(includes_by_taxon)} taxa ({time.monotonic() - t1:.1f}s)"
    )

    if not includes_by_taxon:
        print("nothing to apply.")
        return 0

    if args.dry_run:
        print("dry-run: not modifying the dictionary.")
        for taxon_id, names in list(includes_by_taxon.items())[:10]:
            print(f"  example: NCBITaxon_{taxon_id} += {names}")
        return 0

    t2 = time.monotonic()
    conn = sqlite3.connect(args.dict_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # delta-only

    entries_touched = 0
    new_synonyms_added = 0
    new_inverse_rows = 0
    ambiguity_captures = 0
    missing_entries = 0

    for taxon_id, names in includes_by_taxon.items():
        iri = f"{_NCBITAXON_PREFIX}{taxon_id}"
        row = conn.execute(
            "SELECT entity_type, synonyms_json FROM entries WHERE canonical_iri = ?",
            (iri,),
        ).fetchone()
        if row is None:
            # Should not happen — SC-A4 created an entry for every
            # virus-subtree taxon. Count and move on; surfacing the
            # count is more useful than crashing.
            missing_entries += 1
            continue
        entity_type, syns_json = row
        existing_syns = set(json.loads(syns_json))
        new_for_entry = [n for n in names if n not in existing_syns]
        if not new_for_entry:
            continue
        merged = list(existing_syns) + new_for_entry
        conn.execute(
            "UPDATE entries SET synonyms_json = ? WHERE canonical_iri = ?",
            (json.dumps(merged), iri),
        )
        entries_touched += 1
        new_synonyms_added += len(new_for_entry)

        # Mirror sqlite_writer.write_entry's inverse_index +
        # ambiguous_surface_forms semantics.
        for surface in new_for_entry:
            normalized = normalize_surface_form(surface)
            if not normalized:
                continue
            existing = conn.execute(
                "SELECT canonical_iri FROM inverse_index "
                "WHERE entity_type = ? AND surface_form_normalized = ?",
                (entity_type, normalized),
            ).fetchone()
            if existing is not None and existing[0] != iri:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ambiguous_surface_forms (
                        entity_type, surface_form_normalized,
                        winning_canonical_iri, alternative_canonical_iri
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (entity_type, normalized, iri, existing[0]),
                )
                ambiguity_captures += 1
            conn.execute(
                """
                INSERT OR REPLACE INTO inverse_index (
                    entity_type, surface_form_normalized, canonical_iri
                ) VALUES (?, ?, ?)
                """,
                (entity_type, normalized, iri),
            )
            new_inverse_rows += 1

    conn.commit()
    conn.close()

    print(
        f"\napplied delta in {time.monotonic() - t2:.1f}s:\n"
        f"  entries touched      : {entries_touched}\n"
        f"  new synonyms added   : {new_synonyms_added}\n"
        f"  inverse_index writes : {new_inverse_rows}\n"
        f"  ambiguity captures   : {ambiguity_captures}\n"
        f"  missing entries      : {missing_entries} "
        f"(taxa with 'includes' rows but no SC-A4 entry — should be 0)"
    )
    print(f"\nTOTAL: {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
