"""Test-only steps for G35 cascade verification.

Two trivial BaseStep subclasses pin the cascade contract:

  Workflow.seed (input data unit)
      |  workflow_seed_to_step_a (DirectLink, auto_transfer)
      v
  step_a.seed -> process() -> step_a.intermediate
      |  step_a_to_step_b (DirectLink, auto_transfer)
      v
  step_b.intermediate -> process() -> step_b.final
      |  step_b_to_workflow_output (DirectLink, auto_transfer)
      v
  Workflow.final (output data unit)

Pre-G35, the LocalExecutor called ``workflow.process({})`` and
persisted the trigger-init status dict
(``{"status": "data_flow_initiated", ...}``) as the run's OUTPUT
artifact. The cascade DID fire in background asyncio tasks, but its
output never reached the artifact. After G35 (``workflow.run({})``),
the artifact carries the resolved workflow output data units plus
``status="completed"`` and the cascade is awaited synchronously.

Source: ``eval_03_nanobrain_gap_inventory.md`` Round 4 G35
(2026-05-09); ``apecx-mcp-integration/docs/development_roadmap.md`` 8.6.

Underscore prefix marks this as a test helper module — not shipped in
``src/`` and not loaded outside the integration test suite.
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep


class G35AppendAStep(BaseStep):
    """First cascade step. Reads ``seed``; writes
    ``intermediate = seed + ":a"``.
    """

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seed = input_data.get("seed", "")
        return {"intermediate": f"{seed}:a"}


class G35AppendBStep(BaseStep):
    """Second cascade step. Reads ``intermediate``; writes
    ``final = intermediate + ":b"``.
    """

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        intermediate = input_data.get("intermediate", "")
        return {"final": f"{intermediate}:b"}


__all__ = ["G35AppendAStep", "G35AppendBStep"]
