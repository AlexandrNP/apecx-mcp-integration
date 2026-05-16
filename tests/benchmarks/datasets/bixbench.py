"""BixBench loader — CONFIGURED, run-gated on capsule + tooling infra.

⚠️ READ THIS FIRST — BixBench is not a drop-in codegen benchmark.

BixBench (https://github.com/Future-House/BixBench,
https://huggingface.co/datasets/futurehouse/BixBench) is a
**computational-biology agentic benchmark**: 205 questions derived
from 60 real published Jupyter notebooks ("capsules"). Each question
asks the agent to perform a multi-step bioinformatics analysis
(DESeq2 differential expression, GO enrichment, RNA-seq processing,
etc.) against a real data capsule and report a numeric / categorical
result.

It has two native modes:

1. **Agentic** — the agent explores the capsule's data files,
   executes Python / R / Bash, and reports a result. Requires the
   Docker image ``futurehouse/bixbench:aviary-notebook-env`` and the
   capsule ``.zip`` files (downloaded per-question via
   ``hf_hub_download``). Native run time: 24-48 hours.
2. **Zero-shot** — the question is posed directly; the model answers
   from knowledge. This is QUESTION-ANSWERING, not code generation.

**Why our 4 codegens cannot run BixBench out of the box**:

* Our codegens (retrieval_grounded, perturbed_consensus,
  integrated_similarity, max_power) produce **Python code that runs
  against assert-style tests**. They are CODE DRAFTERS, not QA agents
  and not multi-language analysis agents.
* The agentic mode needs: (a) the capsule data downloaded + extracted,
  (b) R + Bioconductor (DESeq2, clusterProfiler) — many questions are
  R-native, our sandbox is Python-only, (c) the sandbox to mount the
  capsule data directory.
* The zero-shot mode is QA — a code drafter producing a ``solve()``
  function cannot honestly answer "what is the padj" without the data;
  it would have to hardcode the answer, which the dataset's canary
  string explicitly forbids appearing in training corpora.

**This loader is therefore CONFIGURED but its RUN is GATED.** It:

* Loads the 205 questions' metadata from the HF dataset (works today;
  no capsule download needed for metadata).
* Produces ``BenchmarkProblem`` instances in an AGENTIC-CODEGEN shape:
  "write a Python function ``solve(data_dir)`` that performs the
  analysis against the capsule files in ``data_dir`` and returns the
  result". The ``test_code`` compares ``solve()``'s output against the
  question's ``ideal`` using the question's ``eval_mode``.
* **FAILS LOUDLY** if ``$APECX_BIXBENCH_CAPSULES`` (pointing at a
  directory of extracted capsule folders) is not set. We do NOT
  skip-silently — a missing benchmark prerequisite must be a loud
  error, never a green run with zero problems.

To actually RUN BixBench (separate multi-day infra arc):
1. ``huggingface-cli download futurehouse/BixBench --repo-type dataset``
   to get the capsule zips, extract them to a directory.
2. ``export APECX_BIXBENCH_CAPSULES=/path/to/extracted/capsules``
3. Install the bioinformatics tooling the questions need (pandas,
   scanpy, biopython for Python questions; R + Bioconductor for the
   rest). Our sandbox is Python-only today — R questions need a
   sandbox expansion.

Canonical split: BixBench is **eval-only**. The HF dataset exposes a
single ``train`` split (named "train" but it IS the eval set — there
is no training partition). This loader exposes it as split ``eval``.

Honesty contract: any numbers produced by this loader, once the run
is unblocked, measure "can the drafter write bioinformatics analysis
code against a real capsule" — a real signal, but distinct from
BixBench's published agentic / zero-shot accuracy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

DATASET_NAME = "bixbench"
HF_REPO = "futurehouse/BixBench"

# BixBench is eval-only. The HF "train" split IS the eval set.
_HF_SPLIT_FOR = {"eval": "train"}
_DEFAULT_SPLIT = "eval"


def _capsules_dir() -> Path | None:
    env = os.environ.get("APECX_BIXBENCH_CAPSULES")
    return Path(env) if env else None


# The published CapsuleFolder-<uuid>.zip archives extract to a
# directory whose name is CapsuleData-<uuid>/ or CapsuleNotebook-<uuid>/
# — NOT CapsuleFolder-<uuid>/. The zip name and the inner directory
# name differ. We match the extracted directory by the shared <uuid>.
_PYTHON_ANSWERABLE_EVAL_MODES = frozenset({"str_verifier", "range_verifier"})


def _resolve_capsule_dir(capsules_dir: Path, capsule_zip: str) -> Path | None:
    """Find the extracted capsule directory for a ``CapsuleFolder-<uuid>.zip``.

    Returns the matching directory, or ``None`` if no extracted capsule
    with that ``<uuid>`` is present on disk. Matching is by the shared
    UUID suffix because the inner directory name (``CapsuleData-<uuid>``
    or ``CapsuleNotebook-<uuid>``) does not equal the zip's stem.
    """
    uuid = capsule_zip.replace("CapsuleFolder-", "").replace(".zip", "")
    if not capsules_dir.is_dir():
        return None
    for child in sorted(capsules_dir.iterdir()):
        if child.is_dir() and child.name.endswith(uuid):
            return child
    return None


def load_bixbench(
    split: str | None = None,
    limit: int | None = None,
    exclude: set[str] | None = None,
    *,
    eval_modes: set[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Stream BixBench questions as agentic-codegen BenchmarkProblems.

    FAIL-FAST: raises if the split is unknown OR if
    ``$APECX_BIXBENCH_CAPSULES`` is unset. The capsule data is REQUIRED
    to run any BixBench problem (the analysis operates on real files);
    a loader that yielded problems without the data would produce a
    benchmark that always fails at runtime — a silent-failure shape.

    Args:
        eval_modes: when provided, only rows whose ``eval_mode`` is in
            this set are yielded. The CLI passes
            ``{"str_verifier", "range_verifier"}`` — the
            Python-answerable subset — because ``llm_verifier``
            questions need an LLM judge not wired into the subprocess
            sandbox (their ``test_code`` raises ``NotImplementedError``).
            ``None`` yields every eval_mode.

    A row whose extracted capsule directory is NOT present on disk is
    SKIPPED (with a logged count) — not raised on. This lets the loader
    run against a partial capsule extraction (e.g. only the
    Python-answerable subset's 36 capsules). It STILL FAIL-FASTs if
    *zero* problems are yielded — a fully-empty run is a real error.
    """
    split = split or _DEFAULT_SPLIT
    if split not in _HF_SPLIT_FOR:
        raise ValueError(
            f"BixBench: unknown split {split!r}. Valid: {sorted(_HF_SPLIT_FOR)} "
            f"(default {_DEFAULT_SPLIT!r}). BixBench is eval-only — there is no "
            f"train/val partition."
        )

    capsules = _capsules_dir()
    if capsules is None:
        raise RuntimeError(
            "BixBench RUN is gated: $APECX_BIXBENCH_CAPSULES is not set.\n"
            "BixBench questions operate on real data capsules. Running without "
            "them would produce a benchmark that always fails at runtime.\n"
            "To unblock:\n"
            "  1. huggingface-cli download futurehouse/BixBench "
            "--repo-type dataset --local-dir <dir>\n"
            "  2. extract the CapsuleFolder-*.zip files into <dir> (each "
            "extracts to a CapsuleData-<uuid>/ or CapsuleNotebook-<uuid>/ "
            "directory)\n"
            "  3. export APECX_BIXBENCH_CAPSULES=<dir of extracted capsules>\n"
            "  4. install the bioinformatics tooling the questions need "
            "(many are R / Bioconductor — our Python sandbox needs expansion).\n"
            "See docs/biology_benchmark_extension_plan.md for the full status."
        )
    if not capsules.is_dir():
        raise FileNotFoundError(
            f"BixBench: $APECX_BIXBENCH_CAPSULES points at {capsules}, which is not a directory."
        )

    from datasets import load_dataset  # noqa: PLC0415

    ds = load_dataset(HF_REPO)[_HF_SPLIT_FOR[split]]
    skip = exclude or set()
    yielded = 0
    skipped_eval_mode = 0
    skipped_no_capsule = 0
    for row in ds:
        if limit is not None and yielded >= limit:
            return
        if eval_modes is not None and row.get("eval_mode") not in eval_modes:
            skipped_eval_mode += 1
            continue
        capsule_dir = _resolve_capsule_dir(capsules, row["data_folder"])
        if capsule_dir is None:
            # Capsule not extracted on disk — skip (NOT a silent failure:
            # the count is reported, and a zero-yield run still raises).
            skipped_no_capsule += 1
            continue
        problem = _to_problem(row, capsule_dir=capsule_dir)
        if problem.problem_id in skip:
            continue
        yielded += 1
        yield problem
    if yielded == 0:
        raise RuntimeError(
            f"BixBench: 0 problems yielded from {HF_REPO} split {split!r} "
            f"(skipped_eval_mode={skipped_eval_mode}, "
            f"skipped_no_capsule={skipped_no_capsule}). Either no capsules "
            f"are extracted under {capsules}, or the eval_modes filter "
            f"{sorted(eval_modes) if eval_modes else None} matched nothing. "
            f"Extract the CapsuleFolder-*.zip files (see the gating message "
            f"above) before running BixBench."
        )


def _to_problem(row: dict, *, capsule_dir: Path) -> BenchmarkProblem:
    """Reframe one BixBench question as an agentic-codegen problem.

    The candidate must define ``solve(data_dir)`` — a function that
    performs the analysis against the capsule files under ``data_dir``
    and returns the result. ``test_code`` compares the return value
    against ``ideal`` per the question's ``eval_mode``.

    ``capsule_dir`` is the already-resolved extracted capsule directory
    (see :func:`_resolve_capsule_dir`) — it is guaranteed to exist on
    disk by the caller.
    """
    question_id = row["question_id"]
    capsule_zip = row["data_folder"]  # e.g. CapsuleFolder-<uuid>.zip
    capsule_path = capsule_dir

    eval_mode = row.get("eval_mode", "str_verifier")
    ideal = row.get("ideal")

    prompt = (
        "You are performing a computational-biology analysis. Write a "
        "function named ``solve`` that takes ONE argument ``data_dir`` "
        "(a path string to a directory of data files) and returns the "
        "analysis result.\n\n"
        f"Research question:\n{row['question']}\n\n"
        f"The data files are in: {capsule_path}\n\n"
        "Return the result in the form the question asks for (a number, "
        "a string, or a True/False). Use only the Python standard library "
        "plus pandas / numpy / scipy / biopython if installed. Do NOT make "
        "network calls."
    )

    # test_code: run solve(data_dir), compare to ideal per eval_mode.
    # Quoting discipline: json.dumps for every embedded literal.
    data_dir_lit = json.dumps(str(capsule_path))
    ideal_lit = json.dumps(str(ideal))
    if eval_mode == "range_verifier":
        # ideal is like "(1.50,1.54)" — parse + range-check.
        verify = (
            "_lo, _hi = _parse_range(" + ideal_lit + ")\n"
            "_val = float(_result)\n"
            "assert _lo <= _val <= _hi, "
            "f'{_val} not in range [{_lo}, {_hi}]'"
        )
        helper = (
            "def _parse_range(s):\n"
            "    s = s.strip().lstrip('(').rstrip(')')\n"
            "    a, b = s.split(',')\n"
            "    return float(a), float(b)\n"
        )
    elif eval_mode == "str_verifier":
        verify = (
            "assert str(_result).strip().lower() == " + ideal_lit + ".strip().lower(), "
            "f'got {_result!r}, expected {" + ideal_lit + "!r}'"
        )
        helper = ""
    else:
        # llm_verifier — needs an LLM judge we don't wire in the
        # subprocess sandbox. FAIL LOUDLY rather than fake a pass.
        verify = (
            "raise NotImplementedError("
            "'BixBench eval_mode=llm_verifier needs an LLM judge; "
            "not wired into the subprocess sandbox. This problem is "
            "configured but not runnable. See "
            "docs/biology_benchmark_extension_plan.md.')"
        )
        helper = ""

    test_code = (
        helper
        + f"_data_dir = {data_dir_lit}\n"
        + "import os\n"
        + "assert os.path.isdir(_data_dir), "
        + "f'capsule data dir missing: {_data_dir}'\n"
        + "_result = solve(_data_dir)\n"
        + verify
    )

    return BenchmarkProblem(
        problem_id=f"bixbench/{split_label(row)}/{question_id}",
        prompt=prompt,
        setup_code="",
        test_code=test_code,
        entry_point="solve",
        metadata={
            "eval_mode": eval_mode,
            "ideal": ideal,
            "categories": row.get("categories"),
            "capsule_uuid": row.get("capsule_uuid"),
            "capsule_zip": capsule_zip,
            "paper": row.get("paper"),
            "version": row.get("version"),
            "run_status": (
                "configured; run gated on capsule data + bioinformatics "
                "tooling + (for llm_verifier) an LLM judge"
            ),
        },
    )


def split_label(row: dict) -> str:  # noqa: ARG001
    """BixBench is eval-only; the canonical label is 'eval'."""
    return "eval"


__all__ = ["DATASET_NAME", "load_bixbench"]
