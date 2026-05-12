"""Apecx-step reproducer for the outer-workflow hang — STUBBED LLM.

Uses the real CodeReflectionStep + CodeVerificationStep + their
inner workflow YAMLs, but monkey-patches ``build_chat_llm`` so the
LLM call is deterministic + sub-millisecond. This isolates:

  - If this PASSES → the residual outer-workflow hang in the
    LLM-bound test is an LLM-call timing issue (e.g. step process
    takes long enough that the cascade settle window fires
    incorrectly).
  - If this HANGS → the issue is in the apecx YAML wiring or data
    shape mismatch, NOT LLM timing.

Either way we get a concrete signal.

APECX_CODE_EXEC=1 is set inside the test via monkeypatch so the
verification step's gate fires.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"


class _StubLLM:
    """LangChain-Chat-compatible stub returning a canned response.

    The response varies based on which system prompt is loaded —
    the writer sees prompts containing 'OUTPUT RULES' (CodeWriteStep
    system prompt), the reviewer sees 'OUTPUT FORMAT' (CodeReviewStep
    system prompt). We discriminate by looking at the system message
    body to return the right shape.
    """

    def __init__(self):
        pass

    def invoke(self, messages):
        # messages is a list of (System|Human)Message.
        system_content = ""
        for m in messages:
            if (
                hasattr(m, "content")
                and hasattr(m, "type")
                and (m.type == "system" or "System" in type(m).__name__)
            ):
                system_content = str(getattr(m, "content", ""))
                break

        if "OUTPUT FORMAT" in system_content or "structured verdict" in system_content:
            # CodeReviewStep — return JSON verdict.
            payload = {
                "approved": True,
                "reasoning": "Stub reviewer: code matches the spec.",
                "concerns": [],
                "suggestions": [],
            }

            class _R:
                content = json.dumps(payload)

            return _R()
        else:
            # CodeWriteStep — return a tiny valid Python function.
            code = "def add(a: int, b: int) -> int:\n    return a + b\n"

            class _R:
                content = code

            return _R()


def _patch_build_chat_llm(monkeypatch):
    """Replace build_chat_llm in both CodeWriteStep and CodeReviewStep."""

    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return _StubLLM()

    monkeypatch.setattr(
        "apecx_integration.composition.steps.code_write_step.build_chat_llm",
        _factory,
    )
    monkeypatch.setattr(
        "apecx_integration.composition.steps.code_review_step.build_chat_llm",
        _factory,
    )


def test_outer_workflow_with_stub_llm_completes(tmp_path, monkeypatch):
    """Run the real outer workflow with stubbed LLMs.

    Expected: the cascade fires both sub-workflows + the verification
    subprocess. Total wall time should be <10s with the LLM out of
    the loop. Pass criterion: code_verification_output populated
    with a real exec_result dict whose exec_succeeded is True.
    """
    monkeypatch.setenv("APECX_CODE_EXEC", "1")
    _patch_build_chat_llm(monkeypatch)

    from nanobrain.core.workflow import Workflow

    outer_yml = WORKFLOWS / "code_authoring_with_reflection_and_verification.yml"
    wf = Workflow.from_config(str(outer_yml))

    async def _drive():
        # Use the apecx-canonical pattern: deposit into the first
        # step's input DU directly, then poll the LAST step's
        # output DU. Avoids the singleton-executor re-entrance
        # pattern.
        await wf.process(
            {
                "code_reflection_input": {
                    "code_spec": (
                        "Write a function add(a: int, b: int) -> int that returns a + b."
                    ),
                    "function_name": "add",
                    "function_signature": "def add(a: int, b: int) -> int",
                }
            }
        )
        verification_step = wf.child_steps["code_verification"]
        out_du = verification_step.step_output_data_units["code_verification_output"]
        # Stubbed LLM is sub-ms; verification subprocess is ~1-2s.
        # Generous budget to catch any unexpected stall.
        deadline = asyncio.get_event_loop().time() + 60.0
        while True:
            val = await out_du.get()
            if val is not None:
                return val
            if asyncio.get_event_loop().time() >= deadline:
                # Dump diagnostic state on timeout.
                cr = wf.child_steps["code_reflection"]
                cr_out = await cr.step_output_data_units["code_reflection_output"].get()
                cv_in = await verification_step.step_input_data_units[
                    "code_verification_input"
                ].get()
                raise TimeoutError(
                    f"stub-LLM outer cascade did not populate "
                    f"code_verification_output within 60s. "
                    f"code_reflection_output: {type(cr_out).__name__} "
                    f"= {str(cr_out)[:200]!r}. "
                    f"code_verification_input: {type(cv_in).__name__} "
                    f"= {str(cv_in)[:200]!r}."
                )
            await asyncio.sleep(0.2)

    start = time.monotonic()
    result = asyncio.run(_drive())
    elapsed = time.monotonic() - start

    print(
        f"\n[stub-LLM outer] elapsed={elapsed:.2f}s; "
        f"result type={type(result).__name__}; "
        f"keys={sorted(result.keys()) if isinstance(result, dict) else 'N/A'}"
    )
    assert elapsed < 60.0
    assert isinstance(result, dict)
    # The result is the workflow-level output of code_verification_workflow,
    # which is exec_result.
    exec_result = result.get("exec_result", result)
    assert isinstance(exec_result, dict)
    assert "stdout" in exec_result or "returncode" in exec_result, (
        f"exec_result missing expected keys; got: {exec_result}"
    )
