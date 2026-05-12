"""SciCode loader.

SciCode (Tian et al., 2024 — https://scicode-bench.github.io) is a
benchmark of 65 main scientific problems (test split) decomposed into
288–291 subproblems. Each subproblem has natural-language description,
scientific background, a function signature, and hidden test cases.
SOTA Claude Sonnet 3.5 / GPT-4o land around 25–26% subproblem pass@1.
The benchmark's defining feature is **subproblem dependency**: a
subproblem may call helper functions defined in earlier subproblems
within the same main problem.

Data sources + redaction
------------------------

The official Hugging Face mirror (``SciCode1/SciCode``) ships:

* ``validation`` split (15 main problems, 50 subproblems WITH
  ``ground_truth_code`` populated) — usable today via self-compute.
* ``test``       split (65 main problems, 291 subproblems with
  ``ground_truth_code = None`` for every entry) — requires the
  separately-distributed ``test_data.h5`` file (Google Drive,
  gated, not pip-redistributable) to materialize the ``target``
  expected values referenced by every test case.

This loader therefore exposes two paths:

1. **Validation (self-compute, default).** For each subproblem with
   ``ground_truth_code`` set, we run the gold solution + the test-case
   harness in a subprocess to capture the ``target`` value for each
   assert, pickle the values into the ``setup_code``, and rewrite the
   test cases to dereference those pickled values. Self-contained;
   no external artifact required.

2. **Test (HDF5-gated).** When ``SCICODE_TEST_DATA_H5_PATH`` is set
   to a path to the SciCode ``test_data.h5``, the loader reads the
   per-(problem, subproblem, case) targets from the file and rewrites
   the same way. Without the env var, the loader yields zero problems
   from the test split and emits a warning explaining where to get
   the file.

Dependency context
------------------

For a subproblem at step N, ``setup_code`` is built as:

  required_dependencies   # imports declared at the main-problem level
  + prior_gold[1..N-1]    # gold ``ground_truth_code`` for previous
                          # subproblems (validation split only;
                          # test-split prior_code is the LLM's own
                          # previous solutions when running a
                          # multi-step scaffold, not yet wired)
  + ``_target_<i> = pickle.loads(...)`` lines

The ``test_code`` is the original test cases with ``target``
references rewritten to ``_target_<i>``.

Scoring
-------

Subproblem pass@1: each subproblem becomes one ``BenchmarkProblem``,
yielded independently. Aggregating into per-main-problem pass@1 is
done by the scorer, not the loader.
"""

from __future__ import annotations

import ast
import base64
import logging
import os
import pickle
import re
from collections.abc import Iterator
from pathlib import Path

from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

DATASET_NAME = "scicode"
HF_REPO = "SciCode1/SciCode"

# Subprocess budget when self-computing a target. The gold solution
# itself should be fast (these are scientific kernels, not training
# loops), but a single rogue problem should not stall the whole
# loader. 30s matches the runner's per-problem default.
_SELFCOMPUTE_TIMEOUT_SECONDS: float = 30.0


class _TargetCaptureRewriter(ast.NodeTransformer):
    """AST rewriter that converts ``assert <expr_containing_target>``
    into ``_capture.append(<expr_without_target>)``.

    Handles the two test-case shapes that cover essentially all
    SciCode assertions:

      * ``assert <fn>(<X>, target, ...)``           — Call form
      * ``assert <X> == target`` / ``<X> != target`` — Compare form

    For anything more exotic (``(x == target).all()``, nested target
    refs, multiple targets in one assert), the rewriter records the
    failure on ``self.failed`` and leaves the node untouched; the
    loader then skips the subproblem rather than yielding a broken
    BenchmarkProblem.
    """

    def __init__(self) -> None:
        self.captures_in_order: list[ast.AST] = []
        self.failed: bool = False

    def visit_Assert(self, node: ast.Assert) -> ast.AST:  # noqa: N802
        other = self._extract_non_target_side(node.test)
        if other is None:
            self.failed = True
            return node
        idx = len(self.captures_in_order)
        self.captures_in_order.append(other)
        # Build: _capture.append(<other>)
        new_node = ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="_capture", ctx=ast.Load()),
                    attr="append",
                    ctx=ast.Load(),
                ),
                args=[other],
                keywords=[],
            )
        )
        # Drop unused `idx` once Python no longer needs it for inline
        # arithmetic — keeping the index argument-free keeps the
        # capture list ordering equal to assert ordering.
        _ = idx
        return ast.copy_location(new_node, node)

    def _extract_non_target_side(self, expr: ast.AST) -> ast.AST | None:
        if isinstance(expr, ast.Call):
            non_target = [a for a in expr.args if not self._is_target(a)]
            if len(non_target) == 1:
                return non_target[0]
            return None
        if isinstance(expr, ast.Compare):
            sides = [expr.left, *expr.comparators]
            non_target = [s for s in sides if not self._is_target(s)]
            if len(non_target) == 1:
                return non_target[0]
        return None

    @staticmethod
    def _is_target(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "target"


def _rewrite_test_case_for_capture(src: str) -> tuple[str, int, bool]:
    """Return ``(rewritten_source, assert_count, ok)``.

    ``ok=True`` only when every assert in ``src`` references ``target``
    and is one of the two recognized shapes. ``assert_count`` is the
    count of asserts in the original (whether ok or not).
    """
    tree = ast.parse(src)
    assert_count = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Assert))
    rewriter = _TargetCaptureRewriter()
    new_tree = rewriter.visit(tree)
    ast.fix_missing_locations(new_tree)
    if rewriter.failed:
        return src, assert_count, False
    if len(rewriter.captures_in_order) != assert_count:
        return src, assert_count, False
    return ast.unparse(new_tree), assert_count, True


def _rewrite_test_case_for_assert(src: str, target_var_per_assert: list[str]) -> str:
    """Replace each ``target`` reference inside an Assert with the
    next variable name in ``target_var_per_assert``.

    Walk asserts in source order; for the i-th assert, substitute
    every ``Name('target')`` within that assert's expression with
    ``Name(target_var_per_assert[i])``. Returns the rewritten source.
    """
    tree = ast.parse(src)
    counter = {"i": 0}

    def _swap_in_expr(expr: ast.AST, new_name: str) -> ast.AST:
        class _Swap(ast.NodeTransformer):
            def visit_Name(self, n: ast.Name) -> ast.AST:  # noqa: N802
                if n.id == "target":
                    return ast.copy_location(ast.Name(id=new_name, ctx=n.ctx), n)
                return n

        return _Swap().visit(expr)

    class _Walker(ast.NodeTransformer):
        def visit_Assert(self, node: ast.Assert) -> ast.AST:  # noqa: N802
            new_name = target_var_per_assert[counter["i"]]
            counter["i"] += 1
            node.test = _swap_in_expr(node.test, new_name)
            return node

    out = _Walker().visit(tree)
    ast.fix_missing_locations(out)
    return ast.unparse(out)


def _selfcompute_targets(
    required_deps: str,
    prior_gold: list[str],
    current_gold: str,
    test_cases: list[str],
) -> list[bytes] | None:
    """Run the gold solution + a capture-rewritten test harness in a
    subprocess and return one pickle bytestring per assert per test
    case, in original order. Returns ``None`` when:

      * any test case's rewrite fails (sub-problem skipped), OR
      * the subprocess crashes / times out (sub-problem skipped).
    """
    rewritten_cases: list[tuple[str, int]] = []
    for tc in test_cases:
        try:
            rewritten_src, n_asserts, ok = _rewrite_test_case_for_capture(tc)
        except SyntaxError:
            return None
        if not ok:
            return None
        rewritten_cases.append((rewritten_src, n_asserts))

    # Build a single script that imports deps, defines prior + current
    # gold, then runs every test case in a fresh local namespace each
    # time (so test_case[0]'s names do not leak into test_case[1]).
    prelude_lines: list[str] = [
        "import pickle, sys",
        required_deps,
        *prior_gold,
        current_gold,
    ]
    blocks: list[str] = []
    for i, (rewritten, _) in enumerate(rewritten_cases):
        # Each test case runs in its own ``exec`` namespace so the
        # body's local variables (e.g. ``r1``) do not collide across
        # cases. We pre-seed the namespace with ``_capture = []`` and
        # collect it back out.
        blocks.append(
            f"_case_ns_{i}: dict = dict(_globals)\n"
            f"_case_ns_{i}['_capture'] = []\n"
            f"exec({rewritten!r}, _case_ns_{i})\n"
            f"_all_captures.append(_case_ns_{i}['_capture'])\n"
        )

    script = (
        "\n".join(prelude_lines)
        + "\n_globals = dict(globals())\n"
        + "_all_captures: list = []\n"
        + "".join(blocks)
        + "\nsys.stdout.buffer.write(pickle.dumps(_all_captures))\n"
    )

    import subprocess  # noqa: PLC0415

    try:
        proc = subprocess.run(
            ["python", "-c", script],
            capture_output=True,
            timeout=_SELFCOMPUTE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        captures = pickle.loads(proc.stdout)
    except Exception:
        return None
    if not isinstance(captures, list):
        return None

    out: list[bytes] = []
    for case_capture in captures:
        if not isinstance(case_capture, list):
            return None
        for value in case_capture:
            out.append(pickle.dumps(value))
    return out


def _to_subproblem_problem(
    main_row: dict,
    sub_idx: int,
    target_bytes_per_case: list[list[bytes]] | None,
) -> BenchmarkProblem | None:
    """Materialize one BenchmarkProblem for ``sub_steps[sub_idx]``.

    Returns ``None`` if we cannot build a self-contained problem
    (missing gold for prior steps, target capture failed, etc.).

    ``target_bytes_per_case[i]`` is the list of pickled targets for
    the i-th test case (one entry per assert in that case). When
    None, the loader is in test-split mode and the caller must
    supply HDF5-sourced targets — currently unimplemented.
    """
    sub_steps = main_row["sub_steps"]
    cur = sub_steps[sub_idx]

    test_cases: list[str] = cur.get("test_cases") or []
    if not test_cases:
        return None
    if target_bytes_per_case is None:
        return None
    if len(target_bytes_per_case) != len(test_cases):
        return None

    # ---- Build the LLM-facing prompt ----
    prompt_parts: list[str] = []
    if main_row.get("problem_description_main"):
        prompt_parts.append(main_row["problem_description_main"])
    if main_row.get("problem_background_main"):
        prompt_parts.append("Background:\n" + main_row["problem_background_main"])
    if cur.get("step_description_prompt"):
        prompt_parts.append("Step task:\n" + cur["step_description_prompt"])
    if cur.get("step_background"):
        prompt_parts.append("Step background:\n" + cur["step_background"])
    if cur.get("function_header"):
        prompt_parts.append(
            "Implement the following function (you may add helper "
            "imports inside the fenced block):\n"
            + cur["function_header"]
            + ("\n" + cur["return_line"] if cur.get("return_line") else "")
        )
    prompt = "\n\n".join(prompt_parts)

    # ---- Entry point parsed from the function header ----
    entry_point = ""
    if cur.get("function_header"):
        m = re.search(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", cur["function_header"])
        if m:
            entry_point = m.group(1)

    # ---- Build setup_code: deps + prior gold + pickled targets ----
    setup_lines: list[str] = ["import pickle, base64"]
    if main_row.get("required_dependencies"):
        setup_lines.append(main_row["required_dependencies"])
    for j in range(sub_idx):
        gold_j = sub_steps[j].get("ground_truth_code")
        if not gold_j:
            # Missing prior gold => we cannot build a self-contained
            # subproblem. Caller skips.
            return None
        setup_lines.append(gold_j)

    # Flat target variables: _target_0, _target_1, ... (one per assert,
    # walking test cases in order). Per-assert names match what
    # _rewrite_test_case_for_assert substitutes.
    flat_target_names: list[str] = []
    for case_idx, case_bytes in enumerate(target_bytes_per_case):
        for assert_idx, blob in enumerate(case_bytes):
            name = f"_target_{case_idx}_{assert_idx}"
            flat_target_names.append(name)
            b64 = base64.b64encode(blob).decode("ascii")
            setup_lines.append(f"{name} = pickle.loads(base64.b64decode({b64!r}))")

    # ---- Build test_code: rewritten test cases with named targets ----
    test_blocks: list[str] = []
    flat_idx = 0
    for case_idx, raw_case in enumerate(test_cases):
        n_asserts = len(target_bytes_per_case[case_idx])
        names_for_case = [f"_target_{case_idx}_{a}" for a in range(n_asserts)]
        flat_idx += n_asserts
        try:
            rewritten = _rewrite_test_case_for_assert(raw_case, names_for_case)
        except (SyntaxError, IndexError):
            return None
        test_blocks.append(rewritten)
    test_code = "\n".join(test_blocks)

    return BenchmarkProblem(
        problem_id=f"scicode/{main_row['problem_id']}/{cur['step_number']}",
        prompt=prompt,
        setup_code="\n".join(setup_lines),
        test_code=test_code,
        entry_point=entry_point,
        metadata={
            "main_problem_id": str(main_row["problem_id"]),
            "step_number": cur["step_number"],
            "split": main_row.get("_split", "validation"),
        },
    )


def load_scicode(
    split: str = "validation",
    limit: int | None = None,
    exclude: set[str] | None = None,
    *,
    selfcompute_targets: bool = True,
    test_data_h5_path: Path | None = None,
) -> Iterator[BenchmarkProblem]:
    """Yield subproblem BenchmarkProblems.

    ``split``:
        * ``"validation"`` (15 problems, ~50 subproblems with gold)
          — the only path that works without an external artifact.
        * ``"test"`` (65 problems, 291 subproblems) — currently
          gated: requires ``test_data_h5_path`` or
          ``SCICODE_TEST_DATA_H5_PATH`` env var. Not yet
          implemented; loader yields zero problems and logs a
          warning when called for ``"test"`` without the env var
          and the path argument is also None.

    ``limit`` caps the count of yielded subproblems (not main
    problems). ``exclude`` is a set of subproblem IDs (the same
    ID strings the loader produces, e.g. ``"scicode/77/77.1"``).

    ``selfcompute_targets`` (validation only): when True, run gold
    solutions in subprocesses at load time to capture target values.
    Adds 1–10 seconds per subproblem to first-load cost; cached by
    HF's standard datasets cache thereafter.
    """
    from datasets import load_dataset  # noqa: PLC0415

    if split == "test":
        env_path = os.environ.get("SCICODE_TEST_DATA_H5_PATH")
        if test_data_h5_path is None and env_path:
            test_data_h5_path = Path(env_path)
        if test_data_h5_path is None:
            log.warning(
                "SciCode test split requires the gated test_data.h5 file. "
                "Set SCICODE_TEST_DATA_H5_PATH or pass test_data_h5_path. "
                "See loader docstring; falling back to zero problems."
            )
            return
        # Test-split path not yet implemented. Surface the fact loud
        # rather than silently yielding nothing under "looks-working" log.
        raise NotImplementedError(
            "SciCode test-split HDF5-target path is not implemented yet. "
            "Use split='validation' for now. See "
            "docs/composer_codegen_uplift_plan.md CGU-P1-T1 follow-up."
        )

    ds = load_dataset(HF_REPO, split=split)
    skip = exclude or set()
    yielded = 0
    for row in ds:
        # tag the row for downstream metadata without mutating HF cache.
        row_with_split = dict(row)
        row_with_split["_split"] = split

        for sub_idx, sub_step in enumerate(row["sub_steps"]):
            if limit is not None and yielded >= limit:
                return
            if not sub_step.get("ground_truth_code"):
                continue

            test_cases = sub_step.get("test_cases") or []
            if not test_cases:
                continue

            # Self-compute targets via subprocess + AST rewrite.
            prior_gold = [
                row["sub_steps"][j].get("ground_truth_code") or "" for j in range(sub_idx)
            ]
            # Skip if any prior step's gold is missing — we can't
            # build a self-contained problem without the dependency
            # chain.
            if any(not g for g in prior_gold):
                continue

            if not selfcompute_targets:
                # Without targets, no way to score. Skip.
                continue

            captured = _selfcompute_targets(
                required_deps=row.get("required_dependencies") or "",
                prior_gold=prior_gold,
                current_gold=sub_step["ground_truth_code"],
                test_cases=test_cases,
            )
            if captured is None:
                # Either AST rewrite failed or subprocess crashed.
                # Skip honestly; the loader's coverage stats will
                # show this in the smoke output.
                log.info(
                    "scicode: skipped %s/%s/%s (target capture failed)",
                    DATASET_NAME,
                    row["problem_id"],
                    sub_step["step_number"],
                )
                continue

            # Repackage flat captures into per-test-case lists.
            per_case: list[list[bytes]] = []
            cursor = 0
            for tc in test_cases:
                # Count asserts via AST (already verified ok above).
                tree = ast.parse(tc)
                n = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Assert))
                per_case.append(captured[cursor : cursor + n])
                cursor += n

            problem = _to_subproblem_problem(row_with_split, sub_idx, per_case)
            if problem is None:
                continue
            if problem.problem_id in skip:
                continue
            yielded += 1
            yield problem


__all__ = ["DATASET_NAME", "load_scicode"]
