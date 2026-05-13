"""Unit tests for SolutionMemoryStep (cross-problem memory)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apecx_integration.composition.steps.solution_memory_step import (
    SolutionMemoryStep,
)


def _stage(
    tmp_path: Path, *, yaml_extras: str = "", store: str | None = None
) -> SolutionMemoryStep:
    store_path = store if store is not None else str(tmp_path / "store.json")
    p = tmp_path / "v.yml"
    p.write_text(f"name: memory_test\nstore_path: '{store_path}'\n" + yaml_extras)
    return SolutionMemoryStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "memory_test"


def test_unknown_mode_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)mode"):
        _stage(tmp_path, yaml_extras="mode: badmode\n")


def test_read_empty_store_passthrough(tmp_path):
    """Missing store file -> no enrichment, no error."""
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"code_spec": "x", "task_category": "step"}))
    assert out["memory_hit"] is False
    assert out["memory_examples_used"] == 0
    assert out["code_spec"] == "x"


def test_read_with_cached_solution(tmp_path):
    """Pre-populate store, read enrichment includes cached code."""
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"step": ["class CachedStep(BaseStep): pass"]}))
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"code_spec": "Write a step", "task_category": "step"}))
    assert out["memory_hit"] is True
    assert "CachedStep" in out["code_spec"]


def test_record_writes_to_store(tmp_path):
    step = _stage(tmp_path, yaml_extras="mode: record\n")
    out = asyncio.run(
        step.process(
            {
                "code_source": "def winner(): return 42",
                "task_category": "mbpp_math",
            }
        )
    )
    assert out["recorded"] is True
    assert out["category"] == "mbpp_math"
    assert out["store_size_after"] == 1

    # Verify on-disk.
    store_path = tmp_path / "store.json"
    data = json.loads(store_path.read_text())
    assert data["mbpp_math"] == ["def winner(): return 42"]


def test_record_then_read_round_trip(tmp_path):
    """Record one solution then read it back."""
    recorder = _stage(tmp_path, yaml_extras="mode: record\n")
    asyncio.run(recorder.process({"code_source": "def f(): return 1", "task_category": "step"}))

    reader = _stage(tmp_path)
    out = asyncio.run(reader.process({"code_spec": "Write a step", "task_category": "step"}))
    assert out["memory_hit"] is True
    assert "def f():" in out["code_spec"]


def test_record_empty_skipped(tmp_path):
    step = _stage(tmp_path, yaml_extras="mode: record\n")
    out = asyncio.run(step.process({"code_source": "", "task_category": "step"}))
    assert out["recorded"] is False


def test_max_per_category_fifo(tmp_path):
    """Beyond max_per_category, old entries drop off."""
    step = _stage(
        tmp_path,
        yaml_extras="mode: record\nmax_per_category: 2\n",
    )
    asyncio.run(step.process({"code_source": "v1", "task_category": "step"}))
    asyncio.run(step.process({"code_source": "v2", "task_category": "step"}))
    asyncio.run(step.process({"code_source": "v3", "task_category": "step"}))
    data = json.loads((tmp_path / "store.json").read_text())
    assert data["step"] == ["v2", "v3"]


def test_corrupt_store_treated_as_empty(tmp_path):
    """Malformed JSON shouldn't break the read path."""
    store_path = tmp_path / "store.json"
    store_path.write_text("not valid json at all { [ ")
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"code_spec": "x", "task_category": "step"}))
    assert out["memory_hit"] is False


def test_examples_on_read_zero_disables_enrichment(tmp_path):
    """When examples_on_read=0, the lookup is a no-op (telemetry only)."""
    store_path = tmp_path / "store.json"
    store_path.write_text(json.dumps({"step": ["class CachedStep: pass"]}))
    step = _stage(tmp_path, yaml_extras="examples_on_read: 0\n")
    out = asyncio.run(step.process({"code_spec": "x", "task_category": "step"}))
    assert out["memory_hit"] is False  # examples_on_read=0 means hit reported as False
    assert "CachedStep" not in out["code_spec"]


def test_record_only_if_pass_blocks_when_no_consensus_pass(tmp_path):
    """Integrated-workflow gate: with the flag on AND voted_passes==0,
    the recorder MUST NOT persist (memory would accumulate noise)."""
    step = _stage(tmp_path, yaml_extras="mode: record\nrecord_only_if_pass: true\n")
    out = asyncio.run(
        step.process(
            {
                "code_source": "def f(): pass",
                "task_category": "step",
                "voted_passes": 0,
            }
        )
    )
    assert out["recorded"] is False


def test_record_only_if_pass_writes_when_consensus_passes(tmp_path):
    """Gate-open path: voted_passes>=1 → recorder writes the solution."""
    step = _stage(tmp_path, yaml_extras="mode: record\nrecord_only_if_pass: true\n")
    out = asyncio.run(
        step.process(
            {
                "code_source": "def f(): return 1",
                "task_category": "step",
                "voted_passes": 1,
            }
        )
    )
    assert out["recorded"] is True


def test_record_only_if_pass_bypassed_when_signal_absent(tmp_path):
    """Single-shot drafter compatibility: when voted_passes is not in
    the input at all, the gate is bypassed (upstream presumed authoritative)."""
    step = _stage(tmp_path, yaml_extras="mode: record\nrecord_only_if_pass: true\n")
    out = asyncio.run(step.process({"code_source": "def f(): return 1", "task_category": "step"}))
    assert out["recorded"] is True
