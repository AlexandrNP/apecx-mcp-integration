"""Smoke tests for the 4 biology-benchmark loaders.

Coverage:
* Open-Rosalind: loads real data across all 3 release splits;
  generated test_code parses + a correct candidate passes.
* BixBench / BioML-bench / BioProBench: FAIL LOUDLY when invoked
  without their respective prerequisites (capsule data / run-enable
  flags). The load-bearing assertion is that NONE of them
  skip-silently — a deferred / gated benchmark must raise, never
  yield zero problems quietly.

These are smoke tests, not full integration tests. The Open-Rosalind
integration test (real LLM sweep) is the benchmark CLI run recorded
in docs/BENCHMARK_EXECUTION_LOG.md.
"""

from __future__ import annotations

import ast

import pytest

from tests.benchmarks.datasets.biomlbench import load_biomlbench
from tests.benchmarks.datasets.bioprobench import load_bioprobench
from tests.benchmarks.datasets.bixbench import load_bixbench
from tests.benchmarks.datasets.open_rosalind import load_open_rosalind

# ---- Open-Rosalind: RUNNABLE (data cloned into data/benchmarks/) ----


def test_open_rosalind_loads_all_splits():
    for split in ("v0", "v1", "holdout"):
        problems = list(load_open_rosalind(split=split))
        assert len(problems) == 8, f"{split}: expected 8 sequence problems, got {len(problems)}"
        for p in problems:
            assert p.entry_point == "solve"
            assert p.problem_id.startswith(f"open_rosalind/{split}/")
            assert p.metadata["adaptation_note"]  # honesty marker present


def test_open_rosalind_unknown_split_fails_loud():
    with pytest.raises(ValueError, match="unknown split"):
        list(load_open_rosalind(split="train"))  # OR has no train split


def test_open_rosalind_test_code_parses_and_a_correct_candidate_passes():
    problems = list(load_open_rosalind(split="v0"))
    seq05 = next(p for p in problems if p.problem_id.endswith("seq-05"))
    # test_code must be valid Python.
    ast.parse(seq05.test_code)
    # A correct candidate for seq-05 (translate ATGCGTACGTAA -> MRT) must pass.
    ns: dict = {}
    candidate = (
        "def solve():\n"
        "    seq = 'ATGCGTACGTAA'\n"
        "    table = {'ATG':'M','CGT':'R','ACG':'T','TAA':'*'}\n"
        "    prot = ''.join(table.get(seq[i:i+3], '') for i in range(0, len(seq), 3))\n"
        "    return 'Protein translation: ' + prot\n"
    )
    exec(candidate, ns)  # noqa: S102 — controlled test input
    exec(seq05.test_code, ns)  # noqa: S102 — must not raise


def test_open_rosalind_test_code_rejects_wrong_candidate():
    problems = list(load_open_rosalind(split="v0"))
    seq05 = next(p for p in problems if p.problem_id.endswith("seq-05"))
    ns: dict = {}
    exec("def solve():\n    return 'wrong answer'\n", ns)  # noqa: S102
    with pytest.raises(AssertionError):
        exec(seq05.test_code, ns)  # noqa: S102


# ---- The 3 gated benchmarks: must FAIL LOUDLY, never skip-silently ----


def test_bixbench_fails_loud_without_capsules(monkeypatch):
    monkeypatch.delenv("APECX_BIXBENCH_CAPSULES", raising=False)
    with pytest.raises(RuntimeError, match="(?i)gated|capsule"):
        list(load_bixbench(limit=1))


def test_biomlbench_fails_loud_when_runs_deferred(monkeypatch):
    monkeypatch.delenv("APECX_BIOMLBENCH_RUN_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="(?i)deferred"):
        list(load_biomlbench(limit=1))


def test_bioprobench_fails_loud_when_runs_deferred(monkeypatch):
    monkeypatch.delenv("APECX_BIOPROBENCH_RUN_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="(?i)deferred"):
        list(load_bioprobench(limit=1))


def test_gated_benchmarks_reject_unknown_splits():
    # Even before the run-gate, an unknown split must fail loud.
    with pytest.raises(ValueError, match="unknown split"):
        list(load_bixbench(split="nonexistent"))
    with pytest.raises(ValueError, match="unknown split"):
        list(load_biomlbench(split="nonexistent"))
    with pytest.raises(ValueError, match="unknown split"):
        list(load_bioprobench(split="nonexistent"))
