"""Problem-exclusion machinery for the benchmark harness.

Two layers of defense:

1. **Hardcoded blocklists** per dataset (see ``datasets/*.py``).
   These are problems we know are pathological in the dataset
   itself — e.g., MBPP/SciCode entries that hang on assertion,
   have nondeterministic tests, or require packages we don't
   want to install. Each entry is one-line-justified at the
   call site.

2. **Prior-run-derived exclusions.** ``load_blocklist_from_results``
   reads a previous run's JSON output and returns the set of
   problem_ids that failed with Timeout or codegen-side error.
   The CLI plumbs this in via ``--exclude-from``. Data-driven —
   no guessing required.

The subprocess sandbox + per-problem timeout is the primary
defense against session interruption. Blocklists are belt-and-
suspenders for the cases where the sandbox surfaces a known-bad
result every time, not just once.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_blocklist_from_results(json_path: Path) -> set[str]:
    """Read a sweep result JSON and return problem_ids that
    failed in modes likely to interrupt or stall a future run.

    Excluded statuses: ``Timeout`` and ``codegen_*`` (LLM-side
    failures). We deliberately do NOT exclude ``AssertionError``
    or other in-sandbox failures — those bucket as cleanly-failed,
    and excluding them would inflate pass@1 in subsequent runs.
    """
    payload = json.loads(Path(json_path).read_text())
    results = payload.get("results") or []
    blocklist: set[str] = set()
    for r in results:
        if r.get("passed"):
            continue
        err_class = r.get("error_class") or ""
        if err_class == "Timeout" or err_class.startswith("codegen_"):
            blocklist.add(r["problem_id"])
    return blocklist


def merge_exclusions(*sources: set[str] | None) -> set[str]:
    """Union of zero or more exclusion sets, treating ``None`` as empty."""
    out: set[str] = set()
    for s in sources:
        if s:
            out.update(s)
    return out


__all__ = ["load_blocklist_from_results", "merge_exclusions"]
