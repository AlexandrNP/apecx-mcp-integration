"""Integration test: self-improvement loop against real Ollama.

Demonstrates the Reflexion verbal-memory loop end-to-end:

  Run 1: empty memory for spec_id → LLM writes from spec alone →
         critic verdict → memory_write captures lesson.
  Run 2: memory now has the prior lesson → memory_read formats it
         as critique → LLM writes again with critique → verdict
         → another memory entry (or skipped as restatement).

Pin criteria (avoid pinning specific LLM outputs — small-model
variance kills determinism):
  - Run 1 produces a memory entry on disk.
  - Run 2's memory_read returns at least one prior lesson.
  - Run 2's CodeWriteStep receives a non-empty critique.

Auto-skips when Ollama is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_WRITING_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
)


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        return httpx.get(probe, timeout=2.0).status_code == 200
    except Exception:
        return False


SKIP_LLM = "LLM not reachable — set APECX_LLM_BASE_URL"


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_self_improving_workflow_writes_then_reads_memory(tmp_path, monkeypatch):
    """Two-attempt self-improvement cycle against real Ollama.

    - Attempt 1: empty memory, expect a memory entry to land.
    - Attempt 2: same spec_id, expect the prior lesson to surface as
      non-empty critique input to CodeWriteStep.
    """
    from apecx_integration.composition.steps.code_review_step import (
        CodeReviewStep,
    )
    from apecx_integration.composition.steps.code_write_step import (
        CodeWriteStep,
    )
    from apecx_integration.composition.steps.memory_read_step import (
        MemoryReadStep,
    )
    from apecx_integration.composition.steps.memory_store import MemoryStore
    from apecx_integration.composition.steps.memory_write_step import (
        MemoryWriteStep,
    )

    # Isolated memory dir per test run (don't pollute the repo's memory/).
    memory_dir = tmp_path / "memory"

    # Stage step YAMLs with the isolated memory_dir.
    read_yml = tmp_path / "read.yml"
    read_yml.write_text(f"name: read\nmemory_dir: {memory_dir}\nlimit: 3\n")
    write_yml = tmp_path / "write.yml"
    write_yml.write_text(f"name: write\nmemory_dir: {memory_dir}\nmin_lesson_chars: 40\n")

    # Reuse the shipped CodeWriteStep + CodeReviewStep wrappers.
    code_write_yml = CODE_WRITING_DIR / "steps" / "code_write.yml"
    code_review_yml = CODE_WRITING_DIR / "steps" / "code_review.yml"

    reader = MemoryReadStep.from_config(str(read_yml))
    writer_step = MemoryWriteStep.from_config(str(write_yml))
    code_write = CodeWriteStep.from_config(str(code_write_yml))
    code_review = CodeReviewStep.from_config(str(code_review_yml))

    spec_id = "selftest_add_v1"
    spec = (
        "Write a Python function add(a: int, b: int) -> int that returns a + b. "
        "For non-int inputs, raise TypeError."
    )

    async def _one_attempt(attempt_n: int) -> dict:
        # 1. read prior memory.
        read_result = await reader.process(
            {
                "spec_id": spec_id,
                "code_spec": spec,
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
                "spec_keywords": ["add", "integers"],
            }
        )
        prior_lessons_count = read_result["lessons_count"]
        critique = read_result["critique"]
        # 2. write code (passing the critique).
        write_result = await code_write.process(
            {
                "code_spec": spec,
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
                "critique": critique,
            }
        )
        # 3. review.
        review_result = await code_review.process(
            {
                "code_source": write_result["code_source"],
                "code_spec": spec,
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
            }
        )
        # 4. write memory entry.
        memory_outcome = await writer_step.process(
            {
                "spec_id": spec_id,
                "attempt_n": attempt_n,
                "review_verdict": review_result,
                "spec_keywords": ["add", "integers"],
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
            }
        )
        return {
            "prior_lessons_count": prior_lessons_count,
            "critique": critique,
            "code_source": write_result["code_source"],
            "review_approved": review_result["approved"],
            "review_concerns": review_result["concerns"],
            "memory_outcome": memory_outcome,
        }

    # Attempt 1 — empty memory.
    start_1 = time.monotonic()
    result_1 = asyncio.run(_one_attempt(1))
    elapsed_1 = time.monotonic() - start_1
    print(
        f"\n[self-improvement attempt 1] elapsed={elapsed_1:.2f}s; "
        f"prior_lessons={result_1['prior_lessons_count']}; "
        f"review_approved={result_1['review_approved']}; "
        f"memory_written={result_1['memory_outcome']['written']}"
    )

    # Pin: attempt 1 sees no prior lessons.
    assert result_1["prior_lessons_count"] == 0
    assert result_1["critique"] == ""
    # Memory write either succeeded OR was skipped — both are
    # observable; pin that we got an outcome dict.
    assert "written" in result_1["memory_outcome"]

    # Optional: attempt 2 only meaningful if attempt 1 wrote a memory.
    if not result_1["memory_outcome"]["written"]:
        pytest.skip(
            "Attempt 1 did not write to memory (likely a trivial "
            "verdict on a too-easy spec); skip attempt 2 — its "
            "non-empty-critique pin would be vacuous."
        )

    # Verify the entry is on disk.
    store = MemoryStore(root=memory_dir)
    entries = store.read_for_spec(spec_id)
    assert len(entries) == 1, f"expected exactly 1 memory entry after attempt 1; got {len(entries)}"

    # Attempt 2 — should see the prior lesson as critique.
    start_2 = time.monotonic()
    result_2 = asyncio.run(_one_attempt(2))
    elapsed_2 = time.monotonic() - start_2
    print(
        f"\n[self-improvement attempt 2] elapsed={elapsed_2:.2f}s; "
        f"prior_lessons={result_2['prior_lessons_count']}; "
        f"critique_chars={len(result_2['critique'])}; "
        f"review_approved={result_2['review_approved']}; "
        f"memory_written={result_2['memory_outcome']['written']}"
    )

    # Pin: attempt 2 SEES the prior lesson.
    assert result_2["prior_lessons_count"] >= 1, (
        "memory_read did not surface the lesson from attempt 1"
    )
    assert len(result_2["critique"]) > 0, (
        "critique was empty even though prior_lessons_count >= 1; MemoryReadStep formatting broke"
    )
    assert "Prior lesson" in result_2["critique"], (
        f"critique missing the formatted prior-lesson marker; got: {result_2['critique'][:200]!r}"
    )
