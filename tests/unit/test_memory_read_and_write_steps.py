"""CW-MEM5 — unit tests for MemoryReadStep + MemoryWriteStep.

Pure-Python; no LLM. Tests cover:
  1. MemoryReadStep loads via from_config + reads from a real store.
  2. MemoryReadStep empty store → empty critique.
  3. MemoryReadStep formats lessons with status markers.
  4. MemoryReadStep keyword fallback when no spec_id match.
  5. MemoryReadStep passthrough fields (code_spec, function_name, ...).
  6. MemoryWriteStep loads via from_config + writes a real file.
  7. MemoryWriteStep derives lesson from review_verdict.
  8. MemoryWriteStep derives lesson from exec_result on failure.
  9. MemoryWriteStep classifies status (pass / fail / partial).
 10. MemoryWriteStep returns written=False when restatement skipped.
 11. MemoryWriteStep raises on missing spec_id.
 12. End-to-end: write → read → read sees written entry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.memory_read_step import MemoryReadStep
from apecx_integration.composition.steps.memory_store import MemoryStore
from apecx_integration.composition.steps.memory_write_step import (
    MemoryWriteStep,
)


def _stage_read_step(tmp_path: Path, memory_dir: Path) -> MemoryReadStep:
    step_yml = tmp_path / "read.yml"
    step_yml.write_text(f"name: read_test\nmemory_dir: {memory_dir}\nlimit: 3\n")
    return MemoryReadStep.from_config(str(step_yml))


def _stage_write_step(tmp_path: Path, memory_dir: Path) -> MemoryWriteStep:
    step_yml = tmp_path / "write.yml"
    step_yml.write_text(f"name: write_test\nmemory_dir: {memory_dir}\n")
    return MemoryWriteStep.from_config(str(step_yml))


# ---------------------------------------------------------------------------
# MemoryReadStep
# ---------------------------------------------------------------------------


def test_read_step_loads_via_from_config(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_read_step(tmp_path, mem)
    assert step.name == "read_test"


def test_read_step_empty_store_returns_empty_critique(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_read_step(tmp_path, mem)
    result = asyncio.run(
        step.process({"spec_id": "never_seen", "spec_keywords": ["x"], "code_spec": "test"})
    )
    assert result["lessons_count"] == 0
    assert result["critique"] == ""
    assert result["lessons_text"] == ""
    # Passthrough preserved.
    assert result["code_spec"] == "test"
    assert result["spec_id"] == "never_seen"


def test_read_step_formats_lessons_with_status_markers(tmp_path):
    mem = tmp_path / "memstore"
    store = MemoryStore(root=mem)
    store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="First attempt failed because the function returned None on n=0.",
    )
    step = _stage_read_step(tmp_path, mem)
    result = asyncio.run(step.process({"spec_id": "proj_a", "code_spec": "build proj_a"}))
    assert result["lessons_count"] == 1
    assert "[FAIL]" in result["lessons_text"]
    assert "First attempt failed" in result["lessons_text"]


def test_read_step_keyword_fallback(tmp_path):
    """No exact spec_id match → fallback to keyword Jaccard."""
    mem = tmp_path / "memstore"
    store = MemoryStore(root=mem)
    store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="A failure-mode lesson long enough to survive the min-chars gate.",
        spec_keywords=["modulo", "loop"],
    )
    step = _stage_read_step(tmp_path, mem)
    result = asyncio.run(
        step.process(
            {
                "spec_id": "proj_unknown",
                "spec_keywords": ["modulo", "extra"],
                "code_spec": "build proj_unknown",
            }
        )
    )
    assert result["lessons_count"] == 1
    assert "long enough" in result["lessons_text"]


def test_read_step_missing_spec_id_AND_keywords_raises(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_read_step(tmp_path, mem)
    with pytest.raises(ValueError, match="spec_id"):
        asyncio.run(step.process({"code_spec": "x"}))


def test_read_step_non_dict_input_raises(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_read_step(tmp_path, mem)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))


# ---------------------------------------------------------------------------
# MemoryWriteStep
# ---------------------------------------------------------------------------


def test_write_step_loads_via_from_config(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    assert step.name == "write_test"


def test_write_step_writes_explicit_lesson(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    result = asyncio.run(
        step.process(
            {
                "spec_id": "proj_a",
                "lesson": "Explicit lesson that is suitably long for the gate to pass.",
                "status": "pass",
            }
        )
    )
    assert result["written"] is True
    assert result["lesson_used"].startswith("Explicit lesson")
    assert Path(result["entry_path"]).exists()


def test_write_step_derives_lesson_from_review_verdict(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    result = asyncio.run(
        step.process(
            {
                "spec_id": "proj_a",
                "review_verdict": {
                    "approved": False,
                    "reasoning": "Function returned None on n=0; spec asks 0.",
                    "concerns": ["base case returns None"],
                    "suggestions": ["return 0 for n=0"],
                },
            }
        )
    )
    assert result["written"] is True
    assert "Function returned None" in result["lesson_used"]
    assert result["status_classified"] == "fail"


def test_write_step_derives_lesson_from_exec_result(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    result = asyncio.run(
        step.process(
            {
                "spec_id": "proj_a",
                "exec_result": {
                    "exec_succeeded": False,
                    "stderr": "Traceback (most recent call last):\n  AssertionError: expected 5, got 6",
                    "returncode": 1,
                },
            }
        )
    )
    assert result["written"] is True
    assert "Runtime failure" in result["lesson_used"]
    assert "AssertionError" in result["lesson_used"]
    assert result["status_classified"] == "fail"


def test_write_step_classifies_pass_when_both_signals_positive(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    result = asyncio.run(
        step.process(
            {
                "spec_id": "proj_a",
                "lesson": "Both review_approved=True and exec_succeeded=True — clean pass on spec.",
                "review_verdict": {
                    "approved": True,
                    "reasoning": "ok",
                    "concerns": [],
                    "suggestions": [],
                },
                "exec_result": {"exec_succeeded": True, "stdout": "", "stderr": ""},
            }
        )
    )
    assert result["status_classified"] == "pass"


def test_write_step_skips_restatement(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    lesson = "A failure lesson that is long enough to pass the min-chars gate."
    first = asyncio.run(step.process({"spec_id": "proj_a", "lesson": lesson}))
    assert first["written"] is True
    # Same lesson again → restatement skip.
    second = asyncio.run(step.process({"spec_id": "proj_a", "lesson": lesson}))
    assert second["written"] is False
    assert "restatement" in second["reason"] or "skipped" in second["reason"]


def test_write_step_missing_spec_id_raises(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    with pytest.raises(ValueError, match="spec_id"):
        asyncio.run(
            step.process({"lesson": "A long enough lesson that would otherwise be accepted."})
        )


def test_write_step_no_lesson_no_signal_raises(tmp_path):
    mem = tmp_path / "memstore"
    step = _stage_write_step(tmp_path, mem)
    with pytest.raises(ValueError, match="no lesson"):
        asyncio.run(step.process({"spec_id": "proj_a"}))


# ---------------------------------------------------------------------------
# End-to-end: write → read
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips(tmp_path):
    mem = tmp_path / "memstore"
    writer = _stage_write_step(tmp_path, mem)
    reader = _stage_read_step(tmp_path, mem)
    asyncio.run(
        writer.process(
            {
                "spec_id": "fizzbuzz_v1",
                "lesson": "Off-by-one in fizzbuzz: loop bounds were 0..n-1 instead of 1..n.",
                "status": "fail",
                "spec_keywords": ["fizzbuzz", "loop_bounds"],
                "failure_keywords": ["off_by_one"],
            }
        )
    )
    result = asyncio.run(reader.process({"spec_id": "fizzbuzz_v1", "code_spec": "retry fizzbuzz"}))
    assert result["lessons_count"] == 1
    assert "Off-by-one" in result["lessons_text"]
    assert "[FAIL]" in result["lessons_text"]
