"""CGU-P1-T1 — pin the SciCode loader contract.

The loader does three load-bearing things that the test split's
silent redaction makes easy to get subtly wrong:

1. Rewrite ``assert <fn>(<X>, target)`` and ``assert <X> == target``
   into a capture form so the gold solution can populate target
   values at load time.
2. Walk the sub_steps dependency chain — each subproblem needs gold
   solutions for ALL prior subproblems within the same main problem
   to build a self-contained ``setup_code``.
3. Distinguish the validation split (gold available, self-compute
   works) from the test split (gold redacted, requires
   ``SCICODE_TEST_DATA_H5_PATH``).

These tests pin both the AST-level rewrite primitives (offline, no
HF) and the end-to-end loader behavior (one HF round-trip for the
smoke case).
"""

from __future__ import annotations

import ast
import os

import pytest

from tests.benchmarks.datasets.scicode import (
    _rewrite_test_case_for_assert,
    _rewrite_test_case_for_capture,
    load_scicode,
)

# ---------------------------------------------------------------------------
# AST rewriter — pure, no HF, no subprocess
# ---------------------------------------------------------------------------


def test_capture_rewrite_call_form():
    src = "import numpy as np\nx = np.array([1.0, 2.0])\nassert np.allclose(some_fn(x), target)\n"
    out, n_asserts, ok = _rewrite_test_case_for_capture(src)
    assert ok is True
    assert n_asserts == 1
    # The rewriter must have produced a `_capture.append(some_fn(x))`.
    assert "_capture.append(some_fn(x))" in out


def test_capture_rewrite_compare_form():
    src = "result = solve(1)\nassert result == target\n"
    out, n_asserts, ok = _rewrite_test_case_for_capture(src)
    assert ok is True
    assert n_asserts == 1
    assert "_capture.append(result)" in out


def test_capture_rewrite_multiple_asserts_in_one_case():
    src = "a = 1\nb = 2\nassert np.isclose(my_fn(a), target)\nassert my_fn(b) == target\n"
    out, n_asserts, ok = _rewrite_test_case_for_capture(src)
    assert ok is True
    assert n_asserts == 2
    # Both asserts must be turned into appends.
    assert out.count("_capture.append") == 2


def test_capture_rewrite_skips_exotic_form():
    """When the assert is too exotic to parse (e.g. ``(x ==
    target).all()`` wraps the comparison in another call), the
    rewriter reports ok=False so the loader skips the subproblem
    rather than yield a broken test."""
    src = "assert (my_fn(x) == target).all()\n"
    _, n_asserts, ok = _rewrite_test_case_for_capture(src)
    assert n_asserts == 1
    assert ok is False


def test_capture_rewrite_skips_when_no_target():
    """A test_case that does not reference ``target`` is unusable
    in the self-compute path because we cannot tell which value to
    capture. The rewriter must report ok=False."""
    src = "assert my_fn(1) == 42\n"
    _, n_asserts, ok = _rewrite_test_case_for_capture(src)
    assert n_asserts == 1
    assert ok is False


def test_assert_rewrite_substitutes_named_targets():
    """When the loader has captured targets and writes them into
    setup_code as ``_target_0_0``, ``_target_1_0``, ..., the second
    AST helper must substitute ``target`` with those names in the
    test_code, preserving the rest of the assert verbatim."""
    src = "x = 1\nassert solve(x) == target\ny = 2\nassert solve(y) == target\n"
    out = _rewrite_test_case_for_assert(src, ["_target_0_0", "_target_1_0"])
    assert "_target_0_0" in out
    assert "_target_1_0" in out
    # ``target`` should no longer appear as a bare Name in the rewrite.
    tree = ast.parse(out)
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            assert n.id != "target", f"bare 'target' still in: {out}"


# ---------------------------------------------------------------------------
# End-to-end loader — one HF round-trip, gated on network availability
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hf_available() -> bool:
    """Probe whether HF can be reached. The HF datasets library caches
    aggressively, so this is fast on a warm cache but skips cleanly
    on a clean machine without network."""
    if os.environ.get("APECX_SKIP_HF") == "1":
        return False
    try:
        from datasets import load_dataset  # noqa: PLC0415

        load_dataset("SciCode1/SciCode", split="validation")
    except Exception:
        return False
    return True


def test_loader_yields_validation_subproblems(hf_available):
    if not hf_available:
        pytest.skip(
            "HF SciCode dataset not reachable. Set APECX_SKIP_HF=1 to "
            "silence, or run with network + a HF cache."
        )
    # limit=2 keeps the self-compute cost under ~15s for fast CI feedback.
    problems = list(load_scicode(split="validation", limit=2))
    assert len(problems) == 2

    for p in problems:
        # Self-contained: setup_code must exist (deps + prior gold +
        # pickled targets); test_code must exist (asserts).
        assert p.setup_code, f"empty setup_code for {p.problem_id}"
        assert p.test_code, f"empty test_code for {p.problem_id}"
        # entry_point must be parsed out of the function header.
        assert p.entry_point, f"missing entry_point for {p.problem_id}"
        # Metadata must carry main problem ID + step number for the
        # scorer's per-main-problem aggregation.
        assert "main_problem_id" in p.metadata
        assert "step_number" in p.metadata


def test_loader_gold_solution_passes_generated_tests(hf_available):
    """Strongest regression guard: when we run the dataset's own gold
    solution through the loader-generated setup_code + test_code, it
    must pass. If this fails, our AST rewrite or pickle round-trip is
    corrupting the test contract."""
    if not hf_available:
        pytest.skip("HF SciCode dataset not reachable.")
    from datasets import load_dataset  # noqa: PLC0415

    from tests.benchmarks.sandbox import run_in_subprocess

    ds = load_dataset("SciCode1/SciCode", split="validation")
    gold_map = {}
    for row in ds:
        for ss in row["sub_steps"]:
            gold_map[f"scicode/{row['problem_id']}/{ss['step_number']}"] = (
                ss.get("ground_truth_code") or ""
            )

    problems = list(load_scicode(split="validation", limit=2))
    for p in problems:
        gold = gold_map[p.problem_id]
        result = run_in_subprocess(
            candidate_code=gold,
            setup_code=p.setup_code,
            test_code=p.test_code,
            timeout_seconds=30.0,
        )
        assert result.passed, (
            f"Gold solution failed for {p.problem_id}: stderr={result.stderr[-300:]!r}"
        )


def test_test_split_blocks_without_h5_artifact(monkeypatch):
    """The test split requires SCICODE_TEST_DATA_H5_PATH. When the env
    var is absent and no path is passed, the loader must NOT silently
    yield zero problems. Logging a warning is acceptable; pretending
    to work is not."""
    monkeypatch.delenv("SCICODE_TEST_DATA_H5_PATH", raising=False)
    # With no h5 path: the loader returns early (yields zero), which
    # is the documented behavior. That's a soft fail — the caller's
    # scorer will surface the empty sample.
    problems = list(load_scicode(split="test", limit=5))
    assert problems == []


def test_test_split_with_path_raises_not_implemented(monkeypatch, tmp_path):
    """When the user DOES point at an h5, the loader currently raises
    NotImplementedError loudly. That contract is better than a
    silent fall-through: tells the user the feature is staged but
    not wired."""
    fake_h5 = tmp_path / "test_data.h5"
    fake_h5.write_text("not actually an h5")
    monkeypatch.setenv("SCICODE_TEST_DATA_H5_PATH", str(fake_h5))
    with pytest.raises(NotImplementedError, match="test-split HDF5"):
        list(load_scicode(split="test", limit=1))
