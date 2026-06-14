"""SC-B end-to-end measurement (2026-06-08) — pre/post mining delta.

Runs the mu-virus-list 70 terms against TWO dictionary artifacts (the
pre-mining backup and the current prod) and reports the resolution
path delta per term. Also tallies aggregate harmonization rate.

Usage:

    .venv/bin/python scripts/measure_sc_b_delta.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary import loader as _loader  # noqa: E402
from apecx_integration.synonym_dictionary.enums import EntityType  # noqa: E402
from apecx_integration.synonym_dictionary.lookup import lookup_entity  # noqa: E402

_MU_LIST = (
    Path(__file__).resolve().parents[1] / "tests" / "integration" / "fixtures" / "mu_virus_list.txt"
)


def _probe_all(dict_path: Path, terms: list[str]) -> list[dict]:
    # Force a fresh singleton load by configuring with a new path object.
    _loader._singleton.configure(dict_path)
    # Trip the cache invalidator: setting to a path object different
    # from the prior one resets _NOT_LOADED in the singleton.
    out = []
    for t in terms:
        r = lookup_entity(t, entity_type=EntityType.PATHOGEN)
        out.append(
            {
                "query": t,
                "path": r.path,
                "iri": r.canonical_iri,
                "label": r.canonical_label,
                "n_candidates": len(r.candidates),
                "confidence": round(r.confidence, 3),
            }
        )
    return out


def main() -> int:
    pre = Path(os.path.expanduser("~/.apecx/dictionary/dictionary.sqlite.pre-mined.bak"))
    post = Path(os.path.expanduser("~/.apecx/dictionary/dictionary.sqlite"))
    if not pre.exists():
        print(f"ERROR: pre-mining backup missing: {pre}", file=sys.stderr)
        return 1
    if not post.exists():
        print(f"ERROR: post-mining dict missing: {post}", file=sys.stderr)
        return 1

    terms = [line.strip() for line in _MU_LIST.read_text().splitlines() if line.strip()]
    pre_rows = _probe_all(pre, terms)
    post_rows = _probe_all(post, terms)

    # Per-term diff.
    changes = []
    for p, q in zip(pre_rows, post_rows, strict=False):
        if (p["path"], p["iri"]) != (q["path"], q["iri"]):
            changes.append({"query": p["query"], "pre": p, "post": q})

    from collections import Counter

    pre_paths = Counter(r["path"] for r in pre_rows)
    post_paths = Counter(r["path"] for r in post_rows)

    print(f"Terms: {len(terms)}")
    print(f"\nPath distribution (pre):  {dict(pre_paths)}")
    print(f"Path distribution (post): {dict(post_paths)}")
    pre_fast = pre_paths.get("fast", 0) / len(terms)
    post_fast = post_paths.get("fast", 0) / len(terms)
    print(
        f"\nFast-resolution rate: {pre_fast:.3f} -> {post_fast:.3f} (Δ {post_fast - pre_fast:+.3f})"
    )

    print(f"\nPer-term changes ({len(changes)}):")
    for c in changes:
        pre_tail = (c["pre"]["iri"] or "").rsplit("_", 1)[-1] or "n/a"
        post_tail = (c["post"]["iri"] or "").rsplit("_", 1)[-1] or "n/a"
        print(
            f"  {c['query']:38s}  {c['pre']['path']:>9s}({pre_tail:>10s})"
            f"  ->  {c['post']['path']:>9s}({post_tail:>10s})"
        )
    if not changes:
        print("  (no per-term changes — see full report for synonym set growth)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
