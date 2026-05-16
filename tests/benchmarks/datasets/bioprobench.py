"""BioProBench loader — CONFIGURED, runs DEFERRED per user instruction.

⚠️ READ THIS FIRST.

BioProBench (https://huggingface.co/datasets/BioProBench/BioProBench)
is a **biological-protocol understanding benchmark**. It has 4 task
types, each with a canonical train/test split:

* ``PQA`` — Protocol Question Answering (multiple-choice / fill-in-
  the-blank: "Sedate the pig with midazolam (____ mg/kg)" → "0.3").
* ``ERR`` — protocol Error correction.
* ``ORD`` — protocol step Ordering.
* ``GEN`` — protocol Generation.

Plus base protocol corpora (``Bio-protocol.json``,
``Protocol-exchange.json``, ``Protocol-io.json``).

**Why this loader does not run with our codegens**:

BioProBench is a QUESTION-ANSWERING / TEXT-UNDERSTANDING benchmark.
Its tasks expect a chosen MCQ letter, a corrected protocol step, a
re-ordered step list, or generated protocol text. Our 4 codegens
produce **Python code that runs against assert-style tests** — they
are code drafters, not protocol-QA models. There is no honest
"write a solve() function" framing for "which dose of midazolam"
that doesn't reduce to the model hardcoding the answer from memory.

**The user explicitly instructed: configure BioProBench, but DEFER
its runs.** This loader is the CONFIGURED state:

* It reads BioProBench's canonical train/test split files from the
  HF dataset.
* It produces ``BenchmarkProblem`` instances carrying the question +
  choices + answer.
* It **FAILS LOUDLY** unless ``$APECX_BIOPROBENCH_RUN_ENABLED`` is
  explicitly set to ``1``. We do NOT skip-silently — invoking a
  deferred benchmark must be a loud, deliberate opt-in.
* The ``test_code`` raises ``NotImplementedError`` rather than fake a
  pass: BioProBench grading is MCQ-letter / text-similarity, which is
  not wired into our code-execution sandbox.

Canonical splits: ``test`` (default) and ``train``. The HF dataset
ships ``<TASK>_test.json`` / ``<TASK>_train.json`` per task type. This
loader yields across ALL 4 task types for the chosen split, with the
task type recorded in each problem's metadata.

To unblock runs (a separate arc): decide on a QA-codegen contract
(or a non-codegen evaluation path entirely), wire the MCQ /
text-similarity graders, then set ``APECX_BIOPROBENCH_RUN_ENABLED=1``.
See docs/biology_benchmark_extension_plan.md.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

DATASET_NAME = "bioprobench"
HF_REPO = "BioProBench/BioProBench"

# Canonical splits: each task type ships <TASK>_train.json / <TASK>_test.json.
_TASK_TYPES = ("PQA", "ERR", "ORD", "GEN")
_SPLITS = {"test", "train"}
_DEFAULT_SPLIT = "test"


def load_bioprobench(
    split: str | None = None,
    limit: int | None = None,
    exclude: set[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Stream BioProBench problems across all task types — runs DEFERRED.

    FAIL-FAST on:
      1. Unknown split.
      2. ``$APECX_BIOPROBENCH_RUN_ENABLED != '1'`` — runs are deferred
         per the user's explicit instruction.
      3. HF dataset files unavailable.
    """
    split = split or _DEFAULT_SPLIT
    if split not in _SPLITS:
        raise ValueError(
            f"BioProBench: unknown split {split!r}. Valid: {sorted(_SPLITS)} "
            f"(default {_DEFAULT_SPLIT!r}). BioProBench ships canonical "
            f"train/test splits per task type."
        )

    if os.environ.get("APECX_BIOPROBENCH_RUN_ENABLED") != "1":
        raise RuntimeError(
            "BioProBench runs are DEFERRED (per user instruction: configure, "
            "do not run). This loader will not yield problems until you set "
            "APECX_BIOPROBENCH_RUN_ENABLED=1 AND decide on the evaluation "
            "contract — BioProBench is QA / text-understanding, not code "
            "generation; running our code drafters against it needs an "
            "explicit adaptation decision.\n"
            "BioProBench is CONFIGURED (loader + CLI + docs) but not RUNNABLE. "
            "See docs/biology_benchmark_extension_plan.md."
        )

    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    skip = exclude or set()
    yielded = 0
    for task_type in _TASK_TYPES:
        filename = f"{task_type}_{split}.json"
        try:
            local_path = hf_hub_download(HF_REPO, filename, repo_type="dataset")
        except Exception as e:  # noqa: BLE001
            raise FileNotFoundError(
                f"BioProBench: could not fetch {filename} from {HF_REPO}: {e}"
            ) from e
        records = json.loads(Path(local_path).read_text(encoding="utf-8"))
        for record in records:
            if limit is not None and yielded >= limit:
                return
            problem = _to_problem(record, task_type=task_type, split=split)
            if problem.problem_id in skip:
                continue
            yielded += 1
            yield problem


def _to_problem(record: dict, *, task_type: str, split: str) -> BenchmarkProblem:
    """Build a BenchmarkProblem from a BioProBench record.

    The record schema varies by task type; PQA has
    ``question / answer / choices / type / id``. We carry whatever is
    present into metadata and build a uniform prompt.
    """
    rec_id = record.get("id", "unknown")
    question = record.get("question", "")
    choices = record.get("choices") or []
    answer = record.get("answer", "")

    choices_block = ""
    if choices:
        choices_block = "\n\nChoices:\n" + "\n".join(
            f"  {chr(65 + i)}. {c}" for i, c in enumerate(choices)
        )

    prompt = f"Biological-protocol task (type: {task_type}).\n\n{question}{choices_block}\n"

    # test_code: raise loudly. BioProBench grading is MCQ-letter /
    # text-similarity, not code execution. Faking a pass would be a
    # silent failure; this loader is CONFIGURED, not RUNNABLE.
    test_code = (
        "raise NotImplementedError(\n"
        "    'BioProBench grading is MCQ-letter / text-similarity, not '\n"
        "    'code execution. This loader is CONFIGURED (canonical "
        "train/test '\n"
        "    'splits wired) but the evaluation contract for code drafters '\n"
        "    'is undecided — runs deferred per user instruction. See '\n"
        "    'docs/biology_benchmark_extension_plan.md.'\n"
        ")"
    )

    return BenchmarkProblem(
        problem_id=f"bioprobench/{split}/{task_type}/{rec_id}",
        prompt=prompt,
        setup_code="",
        test_code=test_code,
        entry_point="",
        metadata={
            "split": split,
            "task_type": task_type,
            "answer": answer,
            "choices": choices,
            "record_type": record.get("type"),
            "run_status": (
                "configured; runs DEFERRED per user instruction. Gated on: "
                "an evaluation-contract decision (QA benchmark vs our codegen "
                "surface) + MCQ / text-similarity graders."
            ),
        },
    )


__all__ = ["DATASET_NAME", "load_bioprobench"]
