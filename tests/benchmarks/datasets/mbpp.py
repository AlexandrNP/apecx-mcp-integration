"""MBPP loader.

MBPP is "Mostly Basic Python Problems" — 974 short Python tasks
(sanitized split: 257 test, 90 prompt, 90 validation, 374 train).
Each task is a natural-language description plus 3 assert tests.

We use the ``sanitized`` config because it has cleaner prompts and
fewer trivially-bad ground-truth solutions than the raw split.
The ``test`` split (257 problems) is the canonical eval set.

Reference: https://huggingface.co/datasets/google-research-datasets/mbpp
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tests.benchmarks.types import BenchmarkProblem

DATASET_NAME = "mbpp"
HF_REPO = "google-research-datasets/mbpp"
HF_CONFIG = "sanitized"


# Hardcoded blocklist for MBPP problems known to cause issues in
# the sandbox. Each entry is one-line-justified inline.
#
# Curation rule: only add an ID after it has caused a hang, OOM,
# or other session-affecting behavior on >=2 separate runs. The
# subprocess sandbox + timeout is the primary defense; this list
# is for problems we'd rather not bother re-confirming each sweep.
#
# Format: problem_id string (matches what ``_to_problem`` produces).
MBPP_BLOCKLIST: frozenset[str] = frozenset(
    {
        # No persistent entries yet. The 50-problem mistral-nemo
        # baseline (2026-05-12) completed with zero Timeout / zero
        # session-affecting failures. Populate from prior-run JSON
        # via the CLI's --exclude-from flag rather than hardcoding
        # case-by-case.
    }
)


def load_mbpp(
    split: str = "test",
    limit: int | None = None,
    exclude: set[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Stream MBPP problems as ``BenchmarkProblem`` instances.

    ``limit`` caps how many problems we yield — useful for fast
    smoke runs (e.g., ``limit=20``) vs. full sweeps (``limit=None``).

    ``exclude`` is a set of problem_ids to skip in addition to
    ``MBPP_BLOCKLIST``. Pass user-curated IDs or auto-derived
    ones from a prior run via ``exclusions.load_blocklist_from_results``.

    The combined exclusion does NOT count against ``limit`` — we
    skip the row and keep walking until ``limit`` problems are
    yielded, so callers get the expected sample size.
    """
    from datasets import load_dataset  # noqa: PLC0415

    skip = MBPP_BLOCKLIST | (exclude or set())
    ds = load_dataset(HF_REPO, HF_CONFIG, split=split)
    yielded = 0
    for row in ds:
        if limit is not None and yielded >= limit:
            return
        problem = _to_problem(row)
        if problem.problem_id in skip:
            continue
        yielded += 1
        yield problem


def _to_problem(row: dict) -> BenchmarkProblem:
    """Convert one MBPP row into a BenchmarkProblem.

    ``test_imports`` is prepended to setup_code so the assert tests
    can use them. ``test_list`` is joined into test_code.

    ``entry_point`` is sniffed from the first assert: ``assert
    func(args) == expected`` → ``func``. If the regex misses (some
    asserts are weirder), entry_point stays empty — that's fine,
    most codegens don't need it.
    """
    test_imports = row.get("test_imports") or []
    setup_code = "\n".join(test_imports)

    test_list = row.get("test_list") or []
    test_code = "\n".join(test_list)

    entry_point = ""
    if test_list:
        # Match patterns like ``assert remove_Occ("hello", "l") == "heo"``.
        m = re.search(r"\bassert\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", test_list[0])
        if m:
            entry_point = m.group(1)

    return BenchmarkProblem(
        problem_id=f"mbpp/{row['task_id']}",
        prompt=row["prompt"],
        setup_code=setup_code,
        test_code=test_code,
        entry_point=entry_point,
        metadata={"reference_code": row.get("code", "")},
    )


__all__ = ["DATASET_NAME", "load_mbpp"]
