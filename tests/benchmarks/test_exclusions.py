"""Pin the exclusion-mechanism contract.

These tests don't hit HF datasets — they exercise the pure exclusion
machinery on synthetic results / IDs so they're fast and offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.benchmarks.exclusions import (
    load_blocklist_from_results,
    merge_exclusions,
)


def test_merge_exclusions_handles_none():
    assert merge_exclusions(None, None) == set()
    assert merge_exclusions({"a"}, None) == {"a"}
    assert merge_exclusions({"a"}, {"b"}) == {"a", "b"}


def test_merge_exclusions_unions_overlapping():
    assert merge_exclusions({"a", "b"}, {"b", "c"}) == {"a", "b", "c"}


def test_load_blocklist_skips_passed(tmp_path: Path):
    payload = {
        "results": [
            {"problem_id": "mbpp/1", "passed": True, "error_class": None},
            {"problem_id": "mbpp/2", "passed": True, "error_class": None},
        ],
    }
    p = tmp_path / "results.json"
    p.write_text(json.dumps(payload))
    assert load_blocklist_from_results(p) == set()


def test_load_blocklist_picks_up_timeouts(tmp_path: Path):
    payload = {
        "results": [
            {"problem_id": "mbpp/1", "passed": False, "error_class": "Timeout"},
            {"problem_id": "mbpp/2", "passed": False, "error_class": "AssertionError"},
            {"problem_id": "mbpp/3", "passed": False, "error_class": "codegen_RuntimeError"},
            {"problem_id": "mbpp/4", "passed": True, "error_class": None},
        ],
    }
    p = tmp_path / "results.json"
    p.write_text(json.dumps(payload))
    blocklist = load_blocklist_from_results(p)
    # Timeout and codegen_* — yes. AssertionError — no.
    assert blocklist == {"mbpp/1", "mbpp/3"}


def test_load_blocklist_ignores_non_timeout_runtime_failures(tmp_path: Path):
    """``NameError`` / ``TypeError`` / ``AssertionError`` etc. are
    in-sandbox failure modes that completed cleanly. They are NOT
    excluded — including them would inflate pass@1 on re-runs."""
    payload = {
        "results": [
            {"problem_id": "mbpp/10", "passed": False, "error_class": "NameError"},
            {"problem_id": "mbpp/11", "passed": False, "error_class": "TypeError"},
            {"problem_id": "mbpp/12", "passed": False, "error_class": "SyntaxError"},
        ],
    }
    p = tmp_path / "results.json"
    p.write_text(json.dumps(payload))
    assert load_blocklist_from_results(p) == set()


def test_mbpp_loader_skips_excluded_ids():
    """Belt-and-suspenders: confirm the loader honors the exclude
    arg. This DOES hit HF (small request, 3 problems) — auto-skipped
    when datasets isn't installed.
    """
    datasets = pytest.importorskip("datasets")
    del datasets
    from tests.benchmarks.datasets.mbpp import load_mbpp  # noqa: PLC0415

    # First 3 unfiltered.
    baseline = list(load_mbpp(split="test", limit=3))
    baseline_ids = {p.problem_id for p in baseline}
    assert len(baseline_ids) == 3

    # Now exclude the first one — we should still get 3 problems
    # back, with the excluded one absent.
    excluded_id = baseline[0].problem_id
    filtered = list(load_mbpp(split="test", limit=3, exclude={excluded_id}))
    filtered_ids = {p.problem_id for p in filtered}
    assert len(filtered_ids) == 3
    assert excluded_id not in filtered_ids
