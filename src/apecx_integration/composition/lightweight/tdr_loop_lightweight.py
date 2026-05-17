"""Programmatic (lightweight) variant of tdr_refine_workflow.yml.

Builds the same TDR refine cycle via nanobrain's ``WorkflowBuilder``.
G99b — the alternative authoring path the workspace CLAUDE.md
flagged as a legit "second way":

  1. Hand-authored YAML + ``Workflow.from_config(path)``  — see
     ``workflows/tdr_loop/tdr_refine_workflow.yml``.
  2. ``Workflow.from_skeleton(skeleton, bindings)`` — G9 typed
     bindings, deferred (no skeleton authored yet for this pattern).
  3. **Lightweight programmatic** — this module. Best for
     code-generated workflows where the topology is parameterized by
     runtime values OR where the authoring code can decide at build
     time whether to include the LoopController + ConditionalLinks
     based on caller config.

Functional parity with the YAML build is the explicit design
contract. Both builds use the same TdrIterationStep + LoopController
classes; both wire the same 5 links (1 DirectLink + 4 ConditionalLinks);
both produce the same ``final_code`` output shape. The G99 framework
fixes apply to both equally because they touch the underlying
classes (LoopController.process, ConditionalLink.transfer +
_init_from_config, WorkflowGraph._get_cycles_info).
"""

from __future__ import annotations

from pathlib import Path

from nanobrain.core.workflow import Workflow
from nanobrain.lightweight import WorkflowBuilder

TDR_ITERATION_STEP_CLASS = "apecx_integration.composition.steps.tdr_iteration_step.TdrIterationStep"
LOOP_CONTROLLER_CLASS = "nanobrain.library.steps.loop_controller.LoopController"

# Reuse the wrapper YAMLs from the YAML build so operators tuning a
# step's prompt or timeout via the wrapper automatically see the
# change in both authoring paths.
_REPO_TDR_LOOP_DIR = (
    Path(__file__).resolve().parents[2] / "composition" / "workflows" / "tdr_loop" / "steps"
)
_TDR_ITER_WRAPPER = str(_REPO_TDR_LOOP_DIR / "tdr_iteration.yml")
_LOOP_GATE_WRAPPER = str(_REPO_TDR_LOOP_DIR / "loop_gate.yml")


def build_tdr_refine_workflow(
    *,
    workflow_name: str = "tdr_refine_workflow_lightweight",
    description: str = "TDR refine loop (programmatic build).",
) -> Workflow:
    """Build the TDR refine workflow programmatically.

    The topology mirrors ``tdr_refine_workflow.yml`` exactly:
    ``workflow_input → tdr_iter → ConditionalLink branches → loop_gate
    → back-edge ConditionalLink → tdr_iter (cycle) | final_code``.

    Args:
        workflow_name: Stable name for the constructed workflow. The
            default ends in ``_lightweight`` so an operator can tell
            at a glance whether they're looking at the YAML build or
            the programmatic one in logs / observability.
        description: Workflow-level description (surfaced in
            observability + manifest listings).

    Returns:
        A loaded ``Workflow`` instance ready to ``await .run(...)``.
        Entry: write the initial TDR envelope to the first step's
        input data unit (``tdr_iter_input``). Output: read
        ``final_code`` from the workflow-level outputs.

    Raises:
        Whatever ``WorkflowBuilder.load`` raises — typically
        ``ComponentConfigurationError`` if a step class cannot be
        imported or its inline config is malformed.

    The G18-Step-2 cycle validator allows the back-edge through
    LoopController without ``allow_cycles: true``. The same validator
    extension passes in both the YAML and programmatic builds because
    both build the same WorkflowGraph topology.
    """
    builder = WorkflowBuilder(name=workflow_name, description=description)

    # Two steps: the iteration body + the loop bound.
    builder.add_step(
        "tdr_iter",
        TDR_ITERATION_STEP_CLASS,
        description="One iteration of TDR refine.",
        config=_TDR_ITER_WRAPPER,
    )
    builder.add_step(
        "loop_gate",
        LOOP_CONTROLLER_CLASS,
        description="Iteration bound for TDR refine loop (max 3 iterations).",
        config=_LOOP_GATE_WRAPPER,
    )

    builder.add_input("workflow_input")
    builder.add_output("final_code")

    # Link 1 — initial DirectLink: workflow_input → tdr_iter.
    builder.add_link(
        "workflow_input",
        "tdr_iter.tdr_iter_input",
        link_type="direct",
        auto_transfer=True,
    )

    # Links 2-3 — branch on exec_succeeded.
    builder.add_link(
        "tdr_iter.tdr_iter_output",
        "final_code",
        link_type="conditional",
        condition={"op": "eq", "field": "exec_succeeded", "value": True},
        auto_transfer=True,
    )
    builder.add_link(
        "tdr_iter.tdr_iter_output",
        "loop_gate.loop_gate_input",
        link_type="conditional",
        condition={"op": "eq", "field": "exec_succeeded", "value": False},
        auto_transfer=True,
    )

    # Links 4-5 — branch on allow_continue / loop_exhausted.
    # The first is the BACK-EDGE (creating the cycle); the second
    # routes to final_code on escalation.
    builder.add_link(
        "loop_gate.loop_gate_output",
        "tdr_iter.tdr_iter_input",
        link_type="conditional",
        condition={"op": "eq", "field": "allow_continue", "value": True},
        auto_transfer=True,
    )
    builder.add_link(
        "loop_gate.loop_gate_output",
        "final_code",
        link_type="conditional",
        condition={"op": "eq", "field": "loop_exhausted", "value": True},
        auto_transfer=True,
    )

    # Triggers: both steps fire on input data unit change.
    builder.add_trigger(step_name="tdr_iter", trigger_type="data_updated")
    builder.add_trigger(step_name="loop_gate", trigger_type="data_updated")

    return builder.load()


__all__ = ["build_tdr_refine_workflow"]
