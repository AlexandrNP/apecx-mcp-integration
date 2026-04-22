"""T02 Phase 4 finish: the top-level violin_bvbrc_workflow.yml loads
via ``Workflow.from_config(...)``.

This proves the 6 step-YAMLs compose cleanly. It does NOT prove the
workflow runs end-to-end (four steps are missing wrapper YAMLs; see
the workflow YAML header for the per-step blockers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobrain.core.workflow import Workflow

pytestmark = pytest.mark.integration


WORKFLOW_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
    / "violin_bvbrc_workflow.yml"
)


def test_skeleton_workflow_yaml_loads() -> None:
    """Workflow.from_config on the skeleton YAML completes without
    raising. The step-composition internal shape (where nanobrain
    puts the instantiated steps) is not asserted here because the
    framework's `Workflow` attribute layout isn't documented cleanly
    — an internal-attribute assertion would be fragile. A runtime
    execution test belongs with T01 vertical slice.
    """
    assert WORKFLOW_YAML.is_file(), WORKFLOW_YAML
    workflow = Workflow.from_config(str(WORKFLOW_YAML))
    assert workflow.name == "violin_bvbrc_workflow"
    # The workflow object should be populated (non-null, has a config).
    assert workflow is not None
    assert getattr(workflow, "name", None) == "violin_bvbrc_workflow"
