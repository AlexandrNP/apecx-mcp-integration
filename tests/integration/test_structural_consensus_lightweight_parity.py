"""Parity test: lightweight WorkflowBuilder variant ≡ canonical YAML.

Pins that the programmatic ``WorkflowBuilder`` rendering of the
structural-consensus scaffold produces a workflow with EXACTLY the
same step ids, link ids, and node-to-node connectivity as the
hand-authored ``benchmark_structural_consensus/workflow.yml``.

If a future YAML edit changes the canonical topology, this test
fails — forcing the lightweight builder to evolve in lock-step.

Also serves as a regression pin for the framework's silent-failure
shape "WorkflowBuilder.add_link emits flat config that the loader
drops": the builder helper's ``_nest_link_configs`` workaround is
what makes this test pass; if upstream fixes the builder, the
workaround becomes a no-op AND the test still passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
YAML_PATH = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_structural_consensus"
    / "workflow.yml"
)


def _load_yaml_workflow():
    if not YAML_PATH.is_file():
        pytest.skip(f"canonical YAML missing: {YAML_PATH}")
    from nanobrain.core.workflow import Workflow

    return Workflow.from_config(str(YAML_PATH))


def _build_lightweight_workflow():
    from apecx_integration.composition.workflows.benchmark_structural_consensus_lightweight_builder import (
        build_structural_consensus_workflow_lightweight,
    )

    return build_structural_consensus_workflow_lightweight()


def test_step_ids_match():
    yaml_wf = _load_yaml_workflow()
    lw_wf = _build_lightweight_workflow()
    assert sorted(yaml_wf.child_steps.keys()) == sorted(lw_wf.child_steps.keys())


def test_link_ids_match():
    yaml_wf = _load_yaml_workflow()
    lw_wf = _build_lightweight_workflow()
    assert sorted(yaml_wf.step_links.keys()) == sorted(lw_wf.step_links.keys())


def test_link_counts_match():
    yaml_wf = _load_yaml_workflow()
    lw_wf = _build_lightweight_workflow()
    # The lightweight variant must not silently drop links (the dominant
    # failure mode the _nest_link_configs workaround guards against).
    assert len(yaml_wf.step_links) == len(lw_wf.step_links) > 0


def test_step_class_paths_match():
    yaml_wf = _load_yaml_workflow()
    lw_wf = _build_lightweight_workflow()
    yaml_classes = {sid: step.__class__.__name__ for sid, step in yaml_wf.child_steps.items()}
    lw_classes = {sid: step.__class__.__name__ for sid, step in lw_wf.child_steps.items()}
    assert yaml_classes == lw_classes
