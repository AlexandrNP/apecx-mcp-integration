"""SC-E6 — dictionary growth report.

Reads one or more SQLite dictionary artifacts and prints a side-by-side
table of:

- on-disk size (MB)
- ``entries`` table row count
- ``inverse_index`` row count (total surface forms)
- ``ambiguous_surface_forms`` row count (SC-A5b conflict catalog)
- ``taxon_hierarchy`` / ``merged_taxons`` / ``deleted_taxons`` row counts
  (SC-A4-introduced tables; report 0 / "missing" gracefully on older
  pre-SC-A4 builds)
- dictionary_version + built_at from the BuildManifest

The script accepts multiple paths so a build-vs-prod-vs-archive comparison
is one invocation. The natural before/after pair after the SC-A ship:

    .venv/bin/python scripts/dictionary_size_report.py \\
        ~/.apecx/dictionary/dictionary.sqlite.pre-sc-a4.bak \\
        ~/.apecx/dictionary/dictionary.sqlite

Outputs a Markdown-style table by default (paste-able into the SC-E6
implementation log); ``--json`` for machine-readable.

Cross-references SC-A7's virus-subtree-vs-full decision: the size delta
gives an empirical anchor for "is full NCBI Taxonomy ingest tractable?"
For the virus-subtree 281k taxa build we ship today the answer is yes
(244 MB); a full-NCBI build would scale roughly 10× to ~2.4 GB. Numbers
in the report.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))


_KNOWN_TABLES = (
    "entries",
    "inverse_index",
    "ambiguous_surface_forms",
    "taxon_hierarchy",
    "merged_taxons",
    "deleted_taxons",
)


def _table_rowcount(conn: sqlite3.Connection, table: str) -> int | None:
    """Return row count, or None if the table doesn't exist."""
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row else 0


def _read_manifest(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute("SELECT value FROM manifest WHERE key = 'manifest_json'").fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}


def inspect(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "error": "missing"}
    size_bytes = path.stat().st_size
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        report: dict = {
            "path": str(path),
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "tables": {},
        }
        for tbl in _KNOWN_TABLES:
            report["tables"][tbl] = _table_rowcount(conn, tbl)
        manifest = _read_manifest(conn)
        report["dictionary_version"] = manifest.get("dictionary_version")
        report["built_at"] = manifest.get("built_at")
        report["schema_version"] = manifest.get("schema_version")
        # SC-A4-specific manifest fields when present.
        rc_per_type = manifest.get("record_counts_per_entity_type")
        if rc_per_type:
            report["record_counts_per_entity_type"] = rc_per_type
        if "unresolved_count" in manifest:
            report["unresolved_count"] = manifest["unresolved_count"]
    finally:
        conn.close()
    return report


def _fmt_count(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _print_markdown(reports: list[dict]) -> None:
    labels = [Path(r["path"]).name for r in reports]
    header = "| metric |" + "|".join(f" {lbl} " for lbl in labels) + "|"
    sep = "|---|" + "|".join("---" for _ in labels) + "|"
    print(header)
    print(sep)

    def row(name: str, vals: list[str]) -> None:
        print("| " + name + " |" + "|".join(f" {v} " for v in vals) + "|")

    row("size (MB)", [f"{r.get('size_mb')}" if "error" not in r else "missing" for r in reports])
    row("dictionary_version", [str(r.get("dictionary_version") or "—") for r in reports])
    row("built_at", [str(r.get("built_at") or "—") for r in reports])
    row("schema_version", [str(r.get("schema_version") or "—") for r in reports])
    for tbl in _KNOWN_TABLES:
        row(
            f"`{tbl}` rows",
            [_fmt_count(r.get("tables", {}).get(tbl)) for r in reports],
        )
    row("unresolved_count", [str(r.get("unresolved_count", "—")) for r in reports])


def _print_deltas(reports: list[dict]) -> None:
    """Brutal-truth delta block: changes between the first and last report."""
    if len(reports) < 2:
        return
    a, b = reports[0], reports[-1]
    if "error" in a or "error" in b:
        return
    print("\n### Delta: " + Path(a["path"]).name + " → " + Path(b["path"]).name)
    print("```")
    delta_mb = b["size_mb"] - a["size_mb"]
    factor = b["size_mb"] / a["size_mb"] if a["size_mb"] else float("inf")
    print(
        f"  size:               {a['size_mb']:>10.2f} MB  ->  "
        f"{b['size_mb']:>10.2f} MB   "
        f"(Δ={delta_mb:+.2f} MB, {factor:.2f}×)"
    )
    for tbl in _KNOWN_TABLES:
        old = a["tables"].get(tbl)
        new = b["tables"].get(tbl)
        if old is None and new is None:
            continue
        old_s = _fmt_count(old) if old is not None else "—"
        new_s = _fmt_count(new) if new is not None else "—"
        if old is not None and new is not None:
            d = new - old
            print(f"  {tbl:25s} {old_s:>10s}  ->  {new_s:>10s}  (Δ={d:+d})")
        else:
            print(f"  {tbl:25s} {old_s:>10s}  ->  {new_s:>10s}")
    print("```")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dictionary_size_report")
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more SQLite dictionary paths to inspect.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a Markdown table.",
    )
    args = parser.parse_args(argv)
    reports = [inspect(p) for p in args.paths]
    if args.json:
        print(json.dumps(reports, indent=2))
        return 0
    _print_markdown(reports)
    _print_deltas(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
