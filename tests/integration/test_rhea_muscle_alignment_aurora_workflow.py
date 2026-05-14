"""Tests for the rhea_muscle_alignment_aurora workflow — the minimal demo of
the nanobrain GlobusComputeExecutor step-level ``executor_config`` binding.

Two test surfaces:

  1. **Unconditional** — the Aurora workflow YAML + the
     ``alignment_report_aurora.yml`` step YAML compose cleanly through
     ``Workflow.from_config`` WITHOUT ``$AURORA_GC_ENDPOINT_ID`` set
     (the executor config defaults ``endpoint_id`` to the literal
     "unset"). Asserts the ``alignment_report`` step's ``.executor`` is a
     ``GlobusComputeExecutor`` while ``fasta_collection``'s is the default
     ``LocalExecutor`` — proving the framework fix (step-level
     ``executor_config`` binding) is wired correctly end-to-end through a
     real workflow. No Globus, no network.

  2. **Gated on $AURORA_GC_ENDPOINT_ID** — the full workflow run against a
     live Aurora Globus Compute endpoint. Mirrors the skipif gating in
     nanobrain's ``tests/integration/test_globus_compute_executor_local.py``.
     Auto-skips cleanly when the env var is unset.

The gated test is NOT run in the development environment — no Globus auth
is available here. An operator with a real Aurora endpoint must run it and
record the outcome (workspace policy: a component is not "tested" until an
integration test has run it against real data).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "rhea_muscle_alignment_aurora"
)
WORKFLOW_YAML = WORKFLOW_DIR / "workflow.yml"
STEP_AURORA_YAML = WORKFLOW_DIR / "steps" / "alignment_report_aurora.yml"

_AURORA_ENDPOINT = os.environ.get("AURORA_GC_ENDPOINT_ID")
_skip_no_aurora = pytest.mark.skipif(
    _AURORA_ENDPOINT is None,
    reason="AURORA_GC_ENDPOINT_ID not set — gated live-Aurora test",
)


# ---------------------------------------------------------------------------
# 1. Unconditional — load + executor-binding structure. No network.
# ---------------------------------------------------------------------------
def test_aurora_workflow_loads_without_endpoint_env():
    """The Aurora workflow loads via from_config with $AURORA_GC_ENDPOINT_ID unset.

    The executor config uses the ``${AURORA_GC_ENDPOINT_ID:-unset}`` default
    form, so Workflow.from_config must succeed even with the env var unset —
    the GlobusComputeExecutor object builds at load time; it only reaches the
    network at run time.
    """
    from nanobrain.core.workflow import Workflow

    # Explicitly ensure the env var is unset for this assertion's premise.
    saved = os.environ.pop("AURORA_GC_ENDPOINT_ID", None)
    try:
        wf = Workflow.from_config(str(WORKFLOW_YAML))
    finally:
        if saved is not None:
            os.environ["AURORA_GC_ENDPOINT_ID"] = saved
    assert wf is not None
    steps = wf.child_steps
    assert set(steps) == {"fasta_collection", "muscle_alignment", "alignment_report"}


def test_aurora_workflow_alignment_report_uses_globus_compute_executor():
    """The alignment_report step's executor is a GlobusComputeExecutor.

    This is the end-to-end proof that the framework fix (Deliverable 1:
    BaseStep.resolve_dependencies precedence #2 wires StepConfig.executor_config)
    works through a real workflow: the step YAML declares an executor_config,
    the framework builds + binds a GlobusComputeExecutor.
    """
    from nanobrain.core.distributed.globus_compute_executor import (
        GlobusComputeExecutor,
    )
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(WORKFLOW_YAML))
    alignment_report = wf.child_steps["alignment_report"]
    assert isinstance(alignment_report.executor, GlobusComputeExecutor)
    # The globus_compute block threaded through to the validated config.
    gc_cfg = alignment_report.executor.globus_compute_config
    assert gc_cfg.auth_mode == "client_credentials"
    assert gc_cfg.task_timeout_seconds == 3600
    # endpoint_id is whatever $AURORA_GC_ENDPOINT_ID resolves to, or "unset".
    expected_endpoint = os.environ.get("AURORA_GC_ENDPOINT_ID", "unset")
    assert gc_cfg.endpoint_id == expected_endpoint


def test_aurora_workflow_local_steps_use_local_executor():
    """fasta_collection and muscle_alignment (no executor_config) -> LocalExecutor.

    Confirms the demo is mixed-execution: only alignment_report is dispatched
    to Aurora; the other two steps reuse the unchanged rhea_muscle_alignment
    step configs and run locally on the default executor.
    """
    from nanobrain.core.executor import LocalExecutor
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(WORKFLOW_YAML))
    assert isinstance(wf.child_steps["fasta_collection"].executor, LocalExecutor)
    assert isinstance(wf.child_steps["muscle_alignment"].executor, LocalExecutor)


def test_aurora_step_yaml_loads_standalone():
    """The alignment_report_aurora.yml step loads on its own via from_config."""
    from nanobrain.core.distributed.globus_compute_executor import (
        GlobusComputeExecutor,
    )

    from apecx_integration.composition.steps.alignment_report_step import (
        AlignmentReportStep,
    )

    step = AlignmentReportStep.from_config(str(STEP_AURORA_YAML))
    assert isinstance(step.executor, GlobusComputeExecutor)


# ---------------------------------------------------------------------------
# 2. Gated on $AURORA_GC_ENDPOINT_ID — full run against a live Aurora endpoint.
# ---------------------------------------------------------------------------
@_skip_no_aurora
def test_aurora_workflow_full_run_against_live_endpoint():
    """Run the full workflow with alignment_report dispatched to a live Aurora endpoint.

    Requires, beyond $AURORA_GC_ENDPOINT_ID:
      - $GLOBUS_COMPUTE_CLIENT_ID / $GLOBUS_COMPUTE_CLIENT_SECRET;
      - a reachable Rhea MCP server + Redis for the local muscle_alignment step;
      - nanobrain + apecx_integration importable on the Aurora worker, with
        the alignment_report_aurora.yml step config resolvable on its FS.
    See docs/globus_compute_aurora_runbook.md.
    """
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(WORKFLOW_YAML))

    async def _run():
        return await wf.run({}, timeout=3600, settle_ms=200)

    result = asyncio.run(_run())
    # The workflow-level output carries the AlignmentReportStep result.
    output = result.get("workflow_output") if isinstance(result, dict) else result
    assert output is not None
    assert output.get("n_sequences", 0) > 0
    assert output.get("summary")
