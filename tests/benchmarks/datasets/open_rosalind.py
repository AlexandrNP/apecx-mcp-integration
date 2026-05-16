"""Open-Rosalind loader — codegen-adapted subset.

⚠️ READ THIS FIRST — what this loader IS and IS NOT.

Open-Rosalind (https://github.com/maris205/open-rosalind) is a
**tool-first bio-agent benchmark**. Its native scoring measures
whether an agent invokes the right registered tools (uniprot.search,
pubmed, alphafold, sequence.analyze) and produces evidence-grounded
traces. Its published BioBench v0/v1 scores are *process-aware* —
they check ``checks.evidence_path`` against structured tool output.

Our 4 codegens (retrieval_grounded, perturbed_consensus,
integrated_similarity, max_power) produce **Python code that runs
against assert-style tests**. They do not invoke external tools or
emit traces. Running them against Open-Rosalind's native harness is
a benchmark-shape mismatch.

**This loader therefore exposes a CODEGEN-ADAPTED SUBSET, not the
Open-Rosalind benchmark.** Specifically:

* Only the ``sequence_basic`` category (8 problems per release in
  ``biobench_v0`` / ``biobench_v1``; called ``sequence`` in
  ``holdout30``) is included. Those are pure-computation tasks
  (sequence classification, length, GC%, translation, reverse
  complement) that a Python function CAN solve without tool access.
* The other problems (``literature_search``, ``protein_annotation``,
  ``mutation_effect``, ``protocol_reasoning``, ``workflow``,
  ``follow_up``, ``stress``, ``edge_case``) require live UniProt /
  PubMed / variant-DB access and are OUT OF SCOPE for the codegen
  evaluation. They are NOT silently skipped — they are deliberately
  excluded with this documented rationale.
* Each problem is reframed as: "write a function ``solve()`` that
  returns a string analysis of the sequence; the output must
  mention the expected facts." The scorer is keyword presence —
  Open-Rosalind's own *secondary* check (``expected_keywords`` /
  ``keywords``), NOT its primary ``evidence_path`` check (which we
  cannot replicate without the tool framework).

**Honesty contract**: numbers produced by this loader are NOT
comparable to Open-Rosalind's published BioBench scores. They
measure "can the drafter write pure-computation bioinformatics
code", which is a real and useful signal — but a DIFFERENT signal
than the source benchmark.

Splits (Open-Rosalind has NO train/val/test — it is a fixed-eval
benchmark, "a stable score system, not SOTA"). The loader exposes
the release files as splits:

* ``v0`` (default, CANONICAL) — ``biobench_v0.jsonl``, 32 tasks /
  8 codegen-able. This is "the same 32 tasks" run against every
  Open-Rosalind release per ``benchmark/BENCHMARK.md``.
* ``v1`` — ``biobench_v1.jsonl``, 49 tasks / 8 codegen-able.
  Expanded task set.
* ``holdout`` — ``holdout30.json``, 30 tasks / 8 codegen-able.
  Held-out check set.

Data source: cloned to
``data/benchmarks/open-rosalind/benchmark/`` (workspace root OR
worktree root — the loader probes both). Override the directory via
``$APECX_OPEN_ROSALIND_DATA`` (point at the ``benchmark/`` dir).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

DATASET_NAME = "open_rosalind"

# Split -> (filename, is_jsonl, codegen-able category name, keyword-field name).
# Open-Rosalind's release files use slightly different schemas; this table
# normalizes them.
_SPLITS: dict[str, tuple[str, bool, str, str]] = {
    "v0": ("biobench_v0.jsonl", True, "sequence_basic", "expected_keywords"),
    "v1": ("biobench_v1.jsonl", True, "sequence_basic", "expected_keywords"),
    "holdout": ("holdout30.json", False, "sequence", "keywords"),
}
_DEFAULT_SPLIT = "v0"  # canonical per benchmark/BENCHMARK.md

# The clone may live in the worktree root OR the workspace root (the
# worktree is a git worktree under the workspace).
_REL_DIR = Path("data") / "benchmarks" / "open-rosalind" / "benchmark"
_CANDIDATE_ROOTS = [
    Path(__file__).resolve().parents[3],  # worktree root
    Path(__file__).resolve().parents[4],  # workspace root
]


def _resolve_benchmark_dir() -> Path:
    env = os.environ.get("APECX_OPEN_ROSALIND_DATA")
    if env:
        return Path(env)
    for root in _CANDIDATE_ROOTS:
        candidate = root / _REL_DIR
        if candidate.is_dir():
            return candidate
    return _CANDIDATE_ROOTS[0] / _REL_DIR


def load_open_rosalind(
    split: str | None = None,
    limit: int | None = None,
    exclude: set[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Stream the codegen-adapted pure-computation subset.

    FAIL-FAST: if the data file is missing OR the split is unknown,
    raise immediately. We do NOT skip-silently — a missing benchmark
    dataset must be a loud error, not a green run with zero problems.
    """
    split = split or _DEFAULT_SPLIT
    if split not in _SPLITS:
        raise ValueError(
            f"Open-Rosalind: unknown split {split!r}. "
            f"Valid splits: {sorted(_SPLITS)} (default: {_DEFAULT_SPLIT!r}). "
            f"Open-Rosalind has NO train/val/test — these are release files."
        )
    filename, is_jsonl, codegen_category, kw_field = _SPLITS[split]

    bench_dir = _resolve_benchmark_dir()
    data_path = bench_dir / filename
    if not data_path.is_file():
        raise FileNotFoundError(
            f"Open-Rosalind split {split!r} data not found at {data_path}. "
            f"Clone the benchmark first:\n"
            f"  mkdir -p data/benchmarks && cd data/benchmarks && \\\n"
            f"  git clone --depth 1 https://github.com/maris205/open-rosalind\n"
            f"Or set $APECX_OPEN_ROSALIND_DATA to the benchmark/ directory."
        )

    if is_jsonl:
        records = [
            json.loads(line)
            for line in data_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        records = json.loads(data_path.read_text(encoding="utf-8"))

    skip = exclude or set()
    yielded = 0
    for record in records:
        if limit is not None and yielded >= limit:
            return
        if record.get("category") != codegen_category:
            continue
        problem = _to_problem(record, split=split, kw_field=kw_field)
        if problem.problem_id in skip:
            continue
        yielded += 1
        yield problem


def _to_problem(record: dict, *, split: str, kw_field: str) -> BenchmarkProblem:
    """Reframe one pure-computation record as a codegen problem.

    ``kw_field`` is the schema-specific keyword field name
    (``expected_keywords`` for v0/v1, ``keywords`` for holdout30).
    """
    rec_id = record["id"]
    raw_input = record["input"]
    keywords = record.get(kw_field) or []

    prompt = (
        "You are given a biological sequence (possibly with a FASTA-style "
        "header). Write a function named ``solve`` that takes NO arguments, "
        "analyzes the sequence, and returns a single human-readable string "
        "describing it.\n\n"
        f"Sequence input (verbatim):\n{raw_input!r}\n\n"
        "Your returned string MUST mention these facts about the sequence "
        f"(case-insensitive): {keywords!r}\n\n"
        "Pure computation only — no network calls, no external tools. "
        "Standard library only (you MAY use no imports at all)."
    )

    # test_code: run solve(), assert each keyword present. Numeric
    # keywords get a word-boundary regex to avoid '9' matching '19'
    # (slightly stricter than Open-Rosalind's bare-substring keyword
    # check; still weaker than its evidence_path structured check).
    #
    # Quoting discipline: every embedded literal is json.dumps-encoded
    # so keyword text containing quotes / backslashes can never break
    # the generated assert.
    test_lines = [
        "import re as _re",
        "_out = solve()",
        "assert isinstance(_out, str), 'solve() must return str, got ' + type(_out).__name__",
        "_lo = _out.lower()",
    ]
    for kw in keywords:
        kw_str = str(kw)
        if kw_str.isdigit():
            pattern = json.dumps(r"\b" + kw_str + r"\b")
            msg = json.dumps(f"missing numeric fact {kw_str!r} in output: ")
            test_lines.append(f"assert _re.search({pattern}, _lo), {msg} + repr(_out)")
        else:
            needle = json.dumps(kw_str.lower())
            msg = json.dumps(f"missing fact {kw_str!r} in output: ")
            test_lines.append(f"assert {needle} in _lo, {msg} + repr(_out)")
    test_code = "\n".join(test_lines)

    return BenchmarkProblem(
        problem_id=f"open_rosalind/{split}/{rec_id}",
        prompt=prompt,
        setup_code="",
        test_code=test_code,
        entry_point="solve",
        metadata={
            "split": split,
            "category": record.get("category"),
            "expected_skill": record.get("expected_skill"),
            "expected_keywords": keywords,
            "source_input": raw_input,
            "adaptation_note": (
                "codegen-adapted from Open-Rosalind sequence subset; "
                "NOT comparable to published BioBench scores"
            ),
        },
    )


__all__ = ["DATASET_NAME", "load_open_rosalind"]
