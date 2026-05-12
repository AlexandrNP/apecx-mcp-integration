"""CGU-P1-T6 integration smoke — the nanobrain-workflow-wrapped
codegen produces valid candidate code end-to-end through the trigger
cascade against a real Ollama daemon.

What this pins (that the unit tests do NOT):

* The full Workflow.from_config + initialize + process + wait_for_cascade
  path actually runs. The unit tests mock build_chat_llm; this test
  hits a real model.
* The DataUnitChangeTrigger on ``drafter_input`` actually fires
  when the workflow's process() writes to it. A trigger registration
  regression would silently no-op (the unit tests cannot catch that
  because they bypass the trigger cascade and call step.process()
  directly).
* The DirectLink between ``drafter.drafter_output`` and the workflow's
  output DU auto-transfers (silent-failure shape G7).

The MBPP smoke problem is MBPP/12 ("Sort a list of lists by the sum
of the inner lists"): chosen because mistral-nemo solves it reliably
in ~5–10s on this hardware (verified during CGU-P1-T6 development).
A failure here is the framework, not the model.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DRAFTER_MODEL = "mistral-nemo:latest"

WORKFLOW_YAML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_direct_codegen"
    / "workflow.yml"
)


def _ollama_reachable(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return model in names
    except Exception:
        return False


SKIP_REASON = (
    f"Ollama daemon not reachable at {OLLAMA_URL} or model {DRAFTER_MODEL!r} "
    "not pulled. Run `ollama serve` + `ollama pull mistral-nemo:latest`."
)


@pytest.mark.skipif(
    os.environ.get("APECX_SKIP_LIVE_LLM") == "1",
    reason="APECX_SKIP_LIVE_LLM=1 set",
)
def test_workflow_codegen_produces_parseable_python_on_mbpp_smoke():
    if not _ollama_reachable(DRAFTER_MODEL):
        pytest.skip(SKIP_REASON)

    from tests.benchmarks.codegen.nanobrain_workflow import (  # noqa: PLC0415
        make_nanobrain_workflow_codegen,
    )
    from tests.benchmarks.datasets.mbpp import load_mbpp  # noqa: PLC0415

    codegen = make_nanobrain_workflow_codegen(WORKFLOW_YAML)

    # MBPP/12 is the second problem in the sanitized test split.
    # Skipping the first keeps us robust to occasional
    # cold-cache-induced slowness on the very first item.
    problems = list(load_mbpp(limit=2))
    target = next(p for p in problems if "mbpp/12" in p.problem_id)

    code = codegen(target)

    # Contract: non-empty string of parseable Python. We do NOT
    # assert that the code passes MBPP's tests — that's the
    # benchmark's job, not the framework's. A failure here means
    # the workflow wrap broke (cascade hang, empty DU, trigger
    # didn't fire) — not a model quality issue.
    assert isinstance(code, str), f"expected str, got {type(code).__name__}"
    assert code.strip(), "workflow returned empty code — cascade likely didn't fire"
    try:
        ast.parse(code)
    except SyntaxError as e:
        # Surface the model output for debugging if parse fails. A
        # parse failure is still a valid benchmark outcome (the
        # model emitted prose); the framework wrap did its job.
        pytest.fail(
            f"workflow returned unparseable Python (model drift, not framework bug):\n"
            f"first 300 chars: {code[:300]!r}\n"
            f"SyntaxError: {e}"
        )
