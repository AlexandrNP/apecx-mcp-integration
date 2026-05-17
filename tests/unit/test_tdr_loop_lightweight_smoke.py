"""Smoke tests for the G99b TDR-as-lightweight workflow.

Mirrors ``test_tdr_workflow_smoke.py`` (the YAML-build smoke tests)
but exercises the ``WorkflowBuilder`` programmatic path. The
end-to-end integration coverage (real Ollama, real cycle) is
shared — the YAML build's integration test is sufficient because
the underlying classes are identical; this file's job is to verify
the lightweight builder PRODUCES the same workflow shape.
"""

from __future__ import annotations

import pytest
from nanobrain.library.steps.loop_controller import LoopController

from apecx_integration.composition.lightweight.tdr_loop_lightweight import (
    build_tdr_refine_workflow,
)


def test_lightweight_workflow_constructs_with_correct_topology():
    """The programmatic build returns a Workflow with the same steps
    + links as the YAML build (5 links, 2 steps, loop_gate is a
    LoopController). Without G18 Step 2 + the ConditionalLink
    framework fixes, this would fail at .load() time."""
    wf = build_tdr_refine_workflow()

    assert "tdr_iter" in wf.child_steps
    assert "loop_gate" in wf.child_steps
    assert isinstance(wf.child_steps["loop_gate"], LoopController)
    assert wf.child_steps["loop_gate"].COMPONENT_TYPE == "loop_controller"

    # 5 links (1 DirectLink + 4 ConditionalLinks), matching the YAML build.
    assert len(wf.step_links) == 5


def test_lightweight_workflow_name_is_distinct_from_yaml_build():
    """The default name ends in ``_lightweight`` so an operator can
    tell at a glance from logs which authoring path was used."""
    wf = build_tdr_refine_workflow()
    assert wf.name.endswith("_lightweight")


def test_lightweight_workflow_custom_name_is_honored():
    wf = build_tdr_refine_workflow(workflow_name="my_custom_tdr")
    assert wf.name == "my_custom_tdr"


@pytest.mark.parametrize(
    "expected_class",
    ["TdrIterationStep", "LoopController"],
)
def test_lightweight_workflow_step_classes(expected_class):
    """Spot-check that both step types are materialized to the
    expected classes — protects against future builder refactor
    silently swapping types."""
    wf = build_tdr_refine_workflow()
    found = [type(step).__name__ for step in wf.child_steps.values()]
    assert expected_class in found, (
        f"Expected {expected_class} in {found} — lightweight builder "
        f"must materialize the same classes as the YAML build."
    )
