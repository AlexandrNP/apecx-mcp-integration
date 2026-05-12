"""Pin MBPP loader contract.

We don't want to ship a benchmark harness whose dataset loader
silently degrades when HF restructures the dataset. Each test
makes a network call (the dataset is cached after first run, so
re-runs are local) — they auto-skip when ``datasets`` isn't
installed in the environment.

Marked under ``pytest.mark.benchmark`` so a default ``pytest``
run doesn't pull from HF; the benchmark CI lane opts in.
"""

from __future__ import annotations

import pytest

datasets = pytest.importorskip("datasets")

from tests.benchmarks.datasets.mbpp import load_mbpp  # noqa: E402

pytestmark = pytest.mark.benchmark


def test_load_mbpp_returns_problems():
    problems = list(load_mbpp(split="test", limit=3))
    assert len(problems) == 3
    for p in problems:
        assert p.problem_id.startswith("mbpp/")
        assert p.prompt
        assert p.test_code
        assert "assert" in p.test_code


def test_load_mbpp_extracts_entry_point():
    """The entry-point sniffer pulls the function name from the
    first assert. The fixture (mbpp/11) is ``remove_Occ``."""
    problems = list(load_mbpp(split="test", limit=1))
    assert problems[0].entry_point == "remove_Occ"


def test_load_mbpp_respects_limit():
    problems = list(load_mbpp(split="test", limit=5))
    assert len(problems) == 5
