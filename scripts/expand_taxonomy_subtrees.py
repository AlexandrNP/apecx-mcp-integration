#!/usr/bin/env python3
"""Expand the synonym dictionary beyond the virus subtree (the deeper fix).

The shipped dictionary synthesises NCBI Taxonomy names only for the virus
subtree (SC-A7 decision). VIOLIN:Gene and other corpora carry bacterial,
fungal, protozoan-parasite, helminth, and HOST organisms that therefore do
not resolve (audit 2026-06-09: only 22% of VIOLIN:Gene organisms resolved;
the gap is Homo sapiens host genes + bacteria + Apicomplexa/Kinetoplastida).

This script appends synthetic entries for a TARGETED set of pathogen + host
clades — NOT the full 2.8M-taxon NCBI tree (67% of which is irrelevant
insects/plants/animals). It works on a COPY of the live dictionary, in a
single transaction, then validates before the caller promotes it.

Targeted clades (union ≈ 1.19M taxa, 42% of full NCBI):
  viruses (already present, included for idempotency), bacteria, archaea,
  fungi, Apicomplexa, Kinetoplastida, Amoebozoa, Metamonada, Platyhelminthes,
  Nematoda, + host taxa (Homo sapiens, Mus musculus, Rattus).

Cost note: this grows the dictionary ~4× (244 MB → ~1.1 GB) and the
clean-install download ~4× (47 MB → ~190 MB compressed). That is a
deliberate tradeoff for comprehensive pathogen + host coverage.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/expand_taxonomy_subtrees.py \\
        [--dict ~/.apecx/dictionary/dictionary.sqlite] \\
        [--taxdump ~/.apecx/taxdump] [--out <copy path>] [--version <str>]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Pathogen + host clade roots. Viruses included so a future single-source-of-
# truth rebuild is idempotent; the script skips taxa already in the dict.
_CLADE_ROOTS: dict[str, int] = {
    "viruses": 10239,
    "bacteria": 2,
    "archaea": 2157,
    "fungi": 4751,
    "apicomplexa": 5794,
    "kinetoplastida": 5653,
    "amoebozoa": 554915,
    "metamonada": 2611341,
    "platyhelminthes": 6157,
    "nematoda": 6231,
    "homo_sapiens": 9606,
    "mus_musculus": 10090,
    "rattus": 10116,
}
_NCBITAXON_IRI_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"


def _existing_taxa(con: sqlite3.Connection) -> set[int]:
    """Taxon ids already present in entries (so we skip them)."""
    out: set[int] = set()
    for (iri,) in con.execute(
        "SELECT canonical_iri FROM entries WHERE canonical_iri LIKE ?",
        (f"{_NCBITAXON_IRI_PREFIX}%",),
    ):
        tail = iri.rsplit("_", 1)[-1]
        if tail.isdigit():
            out.add(int(tail))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="expand_taxonomy_subtrees")
    ap.add_argument("--dict", default=str(Path.home() / ".apecx/dictionary/dictionary.sqlite"))
    ap.add_argument("--taxdump", default=str(Path.home() / ".apecx/taxdump"))
    ap.add_argument("--out", default=None, help="output copy path (default: <dict>.expanded)")
    ap.add_argument("--version", default=None, help="new dictionary_version string")
    args = ap.parse_args(argv)

    src = Path(args.dict)
    if not src.exists():
        print(f"dictionary not found: {src}", file=sys.stderr)
        return 2
    taxdump = Path(args.taxdump)
    nodes = taxdump / "nodes.dmp"
    names = taxdump / "names.dmp"
    for p in (nodes, names):
        if not p.exists():
            print(f"taxdump file missing: {p}", file=sys.stderr)
            return 2
    out = Path(args.out) if args.out else src.with_suffix(".sqlite.expanded")
    version = args.version or f"multiclade-{datetime.now(UTC).date().isoformat()}"

    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.hierarchy_loader import (
        NCBI_NAME_CLASSES_INGESTED,
        compute_subtree_descendants_union,
        parse_names_dmp,
    )
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    t0 = time.time()
    print(f"copying {src} -> {out} ...", file=sys.stderr)
    import shutil

    shutil.copy2(src, out)

    # 1. Which taxa are already present (viruses + corpus)?
    probe = sqlite3.connect(out)
    existing = _existing_taxa(probe)
    probe.close()
    print(f"existing taxa in dict: {len(existing):,}", file=sys.stderr)

    # 2. Compute the union subtree for all clade roots (single nodes.dmp pass).
    print("computing clade union from nodes.dmp ...", file=sys.stderr)
    union = compute_subtree_descendants_union(nodes, list(_CLADE_ROOTS.values()))
    new_taxa = union - existing
    print(
        f"union={len(union):,} taxa; new (not already in dict)={len(new_taxa):,}  "
        f"[{time.time()-t0:.0f}s]",
        file=sys.stderr,
    )

    # 3. One pass over names.dmp, collect scientific name + synonyms for new taxa.
    print("scanning names.dmp for new-taxa names ...", file=sys.stderr)
    sci: dict[int, str] = {}
    syn: dict[int, set[str]] = {}
    for rec in parse_names_dmp(names):
        if rec.taxon_id not in new_taxa:
            continue
        if rec.name_class not in NCBI_NAME_CLASSES_INGESTED:
            continue
        if rec.name_class == "scientific name":
            sci.setdefault(rec.taxon_id, rec.name_text)
        syn.setdefault(rec.taxon_id, set()).add(rec.name_text)
    print(f"collected names for {len(sci):,} new taxa  [{time.time()-t0:.0f}s]", file=sys.stderr)

    # 4. Append entries in ONE transaction (the writer auto-commits per call;
    #    we wrap so 800k+ inserts don't pay a per-statement fsync).
    now = datetime.now(UTC)
    writer = SQLiteDictionaryWriter(out)
    con = writer._conn  # noqa: SLF001 — intentional bulk-transaction control
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    written = 0
    con.execute("BEGIN")
    try:
        for taxon_id, scientific in sci.items():
            entry = DictionaryEntry(
                entity_type=EntityType.PATHOGEN,
                canonical_iri=f"{_NCBITAXON_IRI_PREFIX}{taxon_id}",
                canonical_label=scientific,
                synonyms=tuple(sorted(syn.get(taxon_id, set()))),
                ontology=OntologyName.NCBITAXON,
                ontology_version="ncbi-taxdump",
                source_records=(f"ncbi_names_dmp.{taxon_id}",),
                confidence=1.0,
                resolved_at=now,
            )
            writer.write_entry(entry)
            written += 1
            if written % 100_000 == 0:
                print(f"  wrote {written:,} entries  [{time.time()-t0:.0f}s]", file=sys.stderr)
        # Bump the manifest version.
        row = con.execute("SELECT value FROM manifest WHERE key='manifest_json'").fetchone()
        if row:
            m = json.loads(row[0])
            m["dictionary_version"] = version
            con.execute(
                "UPDATE manifest SET value=? WHERE key='manifest_json'", (json.dumps(m),)
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        writer.close()
    print(f"appended {written:,} entries; version -> {version}  [{time.time()-t0:.0f}s]", file=sys.stderr)

    # 5. Validate the expanded dict loads + report counts.
    v = sqlite3.connect(out)
    e = v.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    inv = v.execute("SELECT COUNT(*) FROM inverse_index").fetchone()[0]
    size_mb = out.stat().st_size / (1024 * 1024)
    v.close()
    print(
        f"\n=== expanded dict at {out} ===\n"
        f"  entries:       {e:,}\n"
        f"  inverse_index: {inv:,}\n"
        f"  size:          {size_mb:.0f} MB\n"
        f"  version:       {version}\n"
        f"  elapsed:       {time.time()-t0:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
