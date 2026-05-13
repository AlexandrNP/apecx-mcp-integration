"""CGU-P1-T5 — pin the nanobrain-native loader contract.

The loader walks ``tests/benchmarks/problems/nanobrain_native/`` and
yields one BenchmarkProblem per subdirectory. These tests pin:

1. The shipped problems directory yields at least the categories
   the codegen-uplift plan calls out (step, builder, config, tool).
2. Loader filters by category correctly.
3. Loader respects ``limit``.
4. Malformed problems (missing required files) are skipped with
   a warning, not raised.
5. Optional ``meta.yml`` overrides category and entry_point.

The end-to-end (codegen → sandbox) behavior is exercised by the
direct baseline sweep itself; this unit test is the structural
contract only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.datasets.nanobrain_native import (
    _DEFAULT_PROBLEMS_DIR,
    DATASET_NAME,
    load_nanobrain_native,
)


def test_default_problems_dir_exists():
    assert _DEFAULT_PROBLEMS_DIR.is_dir(), _DEFAULT_PROBLEMS_DIR


def test_loader_yields_at_least_expected_categories():
    """The shipped suite must include at least one problem per
    category named in the plan: step, builder, config, tool. If
    a future edit drops a category, this test surfaces it."""
    problems = list(load_nanobrain_native())
    categories = {p.metadata["category"] for p in problems}
    expected = {"step", "builder", "config", "tool"}
    missing = expected - categories
    assert not missing, f"missing categories in shipped suite: {missing}"


def test_loader_filters_by_category():
    only_step = list(load_nanobrain_native(categories=["step"]))
    assert only_step, "no step-category problems found"
    for p in only_step:
        assert p.metadata["category"] == "step", p.problem_id


def test_loader_respects_limit():
    problems = list(load_nanobrain_native(limit=2))
    assert len(problems) == 2


def test_loader_yields_required_fields():
    """Every yielded problem has the BenchmarkProblem fields the
    runner depends on: problem_id, prompt, test_code. setup_code
    is allowed to be empty; entry_point is allowed to be empty."""
    for p in load_nanobrain_native():
        assert p.problem_id.startswith(f"{DATASET_NAME}/"), p.problem_id
        assert p.prompt.strip(), f"empty prompt: {p.problem_id}"
        assert p.test_code.strip(), f"empty test_code: {p.problem_id}"


def test_loader_skips_dirs_missing_required_files(tmp_path):
    """A subdirectory without ``prompt.md`` + ``test_code.py`` must
    be skipped silently (with a log warning), NOT raised."""
    bad = tmp_path / "broken_problem"
    bad.mkdir()
    (bad / "meta.yml").write_text("category: step\n")
    # No prompt.md, no test_code.py.

    good = tmp_path / "step_ok"
    good.mkdir()
    (good / "prompt.md").write_text("test prompt")
    (good / "test_code.py").write_text("assert True\n")

    problems = list(load_nanobrain_native(problems_dir=tmp_path))
    ids = [p.problem_id for p in problems]
    assert any("step_ok" in i for i in ids)
    assert not any("broken_problem" in i for i in ids)


def test_meta_yml_overrides_category(tmp_path):
    """If meta.yml declares a category, it wins over the directory-
    name prefix."""
    sub = tmp_path / "step_actually_tool"
    sub.mkdir()
    (sub / "prompt.md").write_text("p")
    (sub / "test_code.py").write_text("pass")
    (sub / "meta.yml").write_text("category: tool\n")
    problems = list(load_nanobrain_native(problems_dir=tmp_path))
    assert len(problems) == 1
    assert problems[0].metadata["category"] == "tool"


def test_malformed_meta_yml_does_not_block_loading(tmp_path):
    """A YAML parse error in meta.yml should NOT block the problem
    from loading — meta.yml is supplemental, not required."""
    sub = tmp_path / "step_with_bad_meta"
    sub.mkdir()
    (sub / "prompt.md").write_text("p")
    (sub / "test_code.py").write_text("pass")
    (sub / "meta.yml").write_text("not: valid: yaml: at all:\n  [broken")
    problems = list(load_nanobrain_native(problems_dir=tmp_path))
    assert len(problems) == 1
    # Falls back to directory-name-prefix inference.
    assert problems[0].metadata["category"] == "step"


def test_missing_problems_dir_raises():
    """A nonexistent problems_dir must raise FileNotFoundError loud,
    not silently yield zero problems."""
    with pytest.raises(FileNotFoundError):
        list(load_nanobrain_native(problems_dir=Path("/this/does/not/exist")))
