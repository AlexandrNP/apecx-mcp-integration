"""BioML-bench loader — CONFIGURED, runs DEFERRED per user instruction.

⚠️ READ THIS FIRST.

BioML-bench (https://github.com/science-machine/biomlbench, v0.1-alpha)
is an **agentic biomedical ML-engineering benchmark** built on top of
OpenAI's MLE-bench. Its ~24 tasks span protein engineering
(ProteinGym DMS), single-cell omics (Open Problems), drug discovery
(Polaris / TDCommons), and medical imaging (Kaggle competitions).

An agent reads a task description, accesses the biomedical data,
designs and implements a COMPLETE ML pipeline from scratch, and
produces a file/folder SUBMISSION. Grading runs per-task ML metrics
(AUC, RMSE, modified-Laplace-log-likelihood, etc.) against held-out
private answers.

**Why this loader does not run with our codegens (yet)**:

* Our codegens produce a single ``solve()`` function. BioML-bench
  needs full ML pipelines + a submission file in a task-specific
  format.
* Each task requires a per-task data download from Kaggle / Polaris
  hub / ProteinGym — large datasets, separate auth per source.
* Grading is per-task (each ``config.yaml`` names a different
  ``grader``); replicating those graders is a real harness build.
* The native harness runs agents inside Docker with a time limit
  (some tasks: 16 hours).

**The user explicitly instructed: configure BioML-bench, but DEFER
its runs.** This loader is the CONFIGURED state:

* It reads the canonical task list
  (``experiments/biomlbench_v0.1a.txt``) and each task's
  ``config.yaml`` + ``description.md``.
* It produces ``BenchmarkProblem`` instances carrying the task
  description + metadata.
* It **FAILS LOUDLY** if the cloned repo is missing OR if
  ``$APECX_BIOMLBENCH_RUN_ENABLED`` is not explicitly set to ``1``.
  We do NOT skip-silently — invoking a deferred benchmark must be a
  loud error, never a green run with zero problems.

Canonical split: BioML-bench v0.1-alpha exposes one task set
(``experiments/biomlbench_v0.1a.txt``). Per-task train/test splits
are INTERNAL to each task's data preparation (the agent trains on
``prepared/public``, is graded on ``prepared/private/answers.csv``).
At the loader level the split is the benchmark version, default
``v0.1a``.

To unblock runs (a separate multi-day arc): wire the per-task data
download, the per-task graders, a submission-file codegen contract,
and Docker isolation. Then set ``APECX_BIOMLBENCH_RUN_ENABLED=1``.
See docs/biology_benchmark_extension_plan.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

DATASET_NAME = "biomlbench"

_SPLITS = {"v0.1a": "experiments/biomlbench_v0.1a.txt"}
_DEFAULT_SPLIT = "v0.1a"

_REL_DIR = Path("data") / "benchmarks" / "biomlbench"
_CANDIDATE_ROOTS = [
    Path(__file__).resolve().parents[3],
    Path(__file__).resolve().parents[4],
]


def _resolve_repo_dir() -> Path:
    env = os.environ.get("APECX_BIOMLBENCH_REPO")
    if env:
        return Path(env)
    for root in _CANDIDATE_ROOTS:
        candidate = root / _REL_DIR
        if candidate.is_dir():
            return candidate
    return _CANDIDATE_ROOTS[0] / _REL_DIR


def load_biomlbench(
    split: str | None = None,
    limit: int | None = None,
    exclude: set[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Stream BioML-bench tasks as BenchmarkProblems — runs DEFERRED.

    FAIL-FAST on three conditions:
      1. Unknown split.
      2. ``$APECX_BIOMLBENCH_RUN_ENABLED != '1'`` — runs are deferred
         per the user's explicit instruction; invoking this loader for
         a sweep must be a loud, deliberate opt-in.
      3. The cloned repo / task list is missing.
    """
    split = split or _DEFAULT_SPLIT
    if split not in _SPLITS:
        raise ValueError(
            f"BioML-bench: unknown split {split!r}. Valid: {sorted(_SPLITS)} "
            f"(default {_DEFAULT_SPLIT!r})."
        )

    if os.environ.get("APECX_BIOMLBENCH_RUN_ENABLED") != "1":
        raise RuntimeError(
            "BioML-bench runs are DEFERRED (per user instruction: configure, "
            "do not run). This loader will not yield problems until you set "
            "APECX_BIOMLBENCH_RUN_ENABLED=1 AND wire the per-task data "
            "download + graders + submission-file contract + Docker isolation.\n"
            "BioML-bench is CONFIGURED (loader + CLI + docs) but not RUNNABLE. "
            "See docs/biology_benchmark_extension_plan.md."
        )

    repo = _resolve_repo_dir()
    task_list_path = repo / _SPLITS[split]
    if not task_list_path.is_file():
        raise FileNotFoundError(
            f"BioML-bench task list not found at {task_list_path}. Clone first:\n"
            f"  cd data/benchmarks && git clone --depth 1 "
            f"https://github.com/science-machine/biomlbench\n"
            f"Or set $APECX_BIOMLBENCH_REPO to the cloned repo root."
        )

    skip = exclude or set()
    yielded = 0
    for line in task_list_path.read_text(encoding="utf-8").splitlines():
        task_id = line.strip()
        if not task_id or task_id.startswith("#"):
            continue
        if limit is not None and yielded >= limit:
            return
        problem = _to_problem(repo, task_id, split)
        if problem.problem_id in skip:
            continue
        yielded += 1
        yield problem


def _to_problem(repo: Path, task_id: str, split: str) -> BenchmarkProblem:
    """Build a BenchmarkProblem from a BioML-bench task's config + description."""
    task_dir = repo / "biomlbench" / "tasks" / task_id
    description = ""
    desc_path = task_dir / "description.md"
    if desc_path.is_file():
        description = desc_path.read_text(encoding="utf-8")

    grader = ""
    config_path = task_dir / "config.yaml"
    if config_path.is_file():
        # Avoid a yaml dependency for one field. config.yaml has a
        # top-level ``grader:`` key with a nested ``name:``. Track
        # whether we are inside the grader block: a non-indented line
        # that is not ``grader:`` ends it; the nested ``name:`` while
        # inside it is the grader name we want.
        in_grader_block = False
        for cfg_line in config_path.read_text(encoding="utf-8").splitlines():
            if cfg_line and not cfg_line[0].isspace():
                in_grader_block = cfg_line.strip().startswith("grader:")
                continue
            if in_grader_block and cfg_line.strip().startswith("name:"):
                grader = cfg_line.strip().split(":", 1)[1].strip()
                break

    prompt = (
        "Biomedical ML-engineering task. Build a complete ML pipeline "
        "that reads the task data, trains a model, and produces a "
        "submission file in the task's expected format.\n\n"
        f"Task: {task_id}\n\n"
        f"{description[:4000] if description else '(description file not found)'}\n"
    )

    # test_code intentionally fails loud — BioML-bench grading is per-task
    # ML metrics against held-out private answers, which this loader does
    # NOT wire. Producing a "test" that always passes would be a silent
    # failure; producing one that always fails would be misleading. So we
    # raise a clear NotImplementedError.
    test_code = (
        "raise NotImplementedError(\n"
        "    'BioML-bench grading is per-task ML metrics against held-out '\n"
        "    'private answers. This is CONFIGURED but the grader harness is '\n"
        "    'not wired (runs deferred per user instruction). See '\n"
        "    'docs/biology_benchmark_extension_plan.md.'\n"
        ")"
    )

    return BenchmarkProblem(
        problem_id=f"biomlbench/{split}/{task_id}",
        prompt=prompt,
        setup_code="",
        test_code=test_code,
        entry_point="",
        metadata={
            "split": split,
            "task_id": task_id,
            "grader": grader,
            "data_source": task_id.split("/", 1)[0],
            "run_status": (
                "configured; runs DEFERRED per user instruction. Gated on: "
                "per-task data download + per-task ML graders + submission-file "
                "contract + Docker isolation."
            ),
        },
    )


__all__ = ["DATASET_NAME", "load_biomlbench"]
