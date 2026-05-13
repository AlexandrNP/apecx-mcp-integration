"""Nanobrain-native benchmark loader.

This is the benchmark that measures the actual product, not external
comparators (MBPP, SciCode). Every problem exercises a competency the
composer must produce reliably for scientists to adopt the tool:

* **step_*** — author a ``BaseStep`` subclass with a working
  ``async def process``.
* **yaml_*** — emit a workflow YAML that loads via
  ``Workflow.from_config`` and produces correct output through the
  trigger cascade. The G7 silent-failure shape
  (``auto_transfer=False`` on a DirectLink) is one of these.
* **conditional_*** — workflow with ``ConditionalLink`` routing by a
  predicate.
* **lightweight_*** — programmatic workflow construction via
  ``nanobrain.lightweight.WorkflowBuilder``.
* **config_*** — subclass ``StepConfig`` with a custom field and
  instantiate via ``from_config``.
* **tool_*** — write a ``Tool`` subclass with an ``async def execute``.

Problem directory layout
------------------------

Each problem lives under ``tests/benchmarks/problems/nanobrain_native/<id>/``
with two required files:

* ``prompt.md`` — the LLM-facing description. Verbatim what the
  codegen sees as ``BenchmarkProblem.prompt``.
* ``test_code.py`` — the verifier. Runs in the SAME sandbox namespace
  as the candidate code (concatenated after the candidate by
  ``sandbox.run_in_subprocess``). It may reference symbols the
  candidate defined (e.g., ``UpperStep``) directly.

Optional:

* ``setup_code.py`` — Python code that runs BEFORE the candidate.
  Used for fixtures the candidate's tests will need.
* ``meta.yml`` — overrides for ``problem_id`` (default: directory
  name with ``nanobrain_native/`` prefix), ``entry_point`` (passed
  to the codegen as a hint), and ``category`` (inferred from the
  ``<category>_*`` directory-name prefix when missing).

Real execution, no mocks
------------------------

Per the workspace mocks-carve-out rule, every nanobrain-native
problem's verifier runs the generated artifact end-to-end against
real machinery (real ``BaseStep.from_config``, real
``Workflow.from_config``, real triggers, real cascade). No mocks.
A problem is not "passable" until its verifier runs the real path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

DATASET_NAME = "nanobrain_native"

# Where the problem directories live, relative to the repo root.
_DEFAULT_PROBLEMS_DIR = Path(__file__).resolve().parent.parent / "problems" / "nanobrain_native"


def load_nanobrain_native(
    problems_dir: Path | None = None,
    limit: int | None = None,
    exclude: set[str] | None = None,
    categories: list[str] | None = None,
) -> Iterator[BenchmarkProblem]:
    """Yield ``BenchmarkProblem`` instances from a problems directory.

    ``problems_dir`` defaults to
    ``tests/benchmarks/problems/nanobrain_native/``.

    ``categories`` filters by the directory-name prefix
    (e.g., ``categories=["step", "yaml"]``). When None, all
    problems are yielded.

    Problems are yielded in directory-listing order
    (sorted alphabetically) so sweeps are reproducible. The skip set
    + filter does NOT count against ``limit`` — we keep walking
    until ``limit`` problems are yielded.
    """
    root = problems_dir or _DEFAULT_PROBLEMS_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"nanobrain_native problems directory missing: {root}")

    skip = exclude or set()
    yielded = 0
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        if limit is not None and yielded >= limit:
            return

        problem = _load_problem(sub)
        if problem is None:
            log.warning("nanobrain_native: skipped %s (missing required files)", sub.name)
            continue

        if categories is not None:
            inferred_cat = problem.metadata.get("category", "")
            if inferred_cat not in categories:
                continue

        if problem.problem_id in skip:
            continue

        yielded += 1
        yield problem


def _load_problem(problem_dir: Path) -> BenchmarkProblem | None:
    """Build a BenchmarkProblem from a single problem directory.

    Returns None when required files are missing (prompt.md +
    test_code.py). Optional files (setup_code.py, meta.yml) are
    used when present.
    """
    prompt_path = problem_dir / "prompt.md"
    test_path = problem_dir / "test_code.py"
    if not prompt_path.is_file() or not test_path.is_file():
        return None

    prompt = prompt_path.read_text(encoding="utf-8").strip()
    test_code = test_path.read_text(encoding="utf-8")

    setup_path = problem_dir / "setup_code.py"
    setup_code = setup_path.read_text(encoding="utf-8") if setup_path.is_file() else ""

    meta = _load_meta(problem_dir)

    # Category inferred from the directory-name prefix
    # (``step_uppercase`` → ``step``) unless meta.yml overrides.
    name = problem_dir.name
    inferred_category = name.split("_", 1)[0] if "_" in name else name
    category = meta.get("category", inferred_category)

    problem_id = meta.get("problem_id", f"{DATASET_NAME}/{name}")
    entry_point = meta.get("entry_point", "")

    return BenchmarkProblem(
        problem_id=problem_id,
        prompt=prompt,
        setup_code=setup_code,
        test_code=test_code,
        entry_point=entry_point,
        metadata={"category": category, "dir": str(problem_dir)},
    )


def _load_meta(problem_dir: Path) -> dict:
    """Load optional meta.yml from a problem directory.

    Returns {} when missing. Any parse error logs a warning and
    returns {} — meta.yml is purely supplemental, so a malformed
    one must not block loading the problem.
    """
    meta_path = problem_dir / "meta.yml"
    if not meta_path.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415

        out = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        if not isinstance(out, dict):
            log.warning("nanobrain_native: %s meta.yml is not a dict", problem_dir.name)
            return {}
        return out
    except Exception as e:  # noqa: BLE001
        log.warning(
            "nanobrain_native: failed to parse %s meta.yml: %s",
            problem_dir.name,
            e,
        )
        return {}


__all__ = ["DATASET_NAME", "load_nanobrain_native"]
