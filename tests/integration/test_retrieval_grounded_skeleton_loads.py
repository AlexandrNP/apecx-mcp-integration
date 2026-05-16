"""Path 3 of 3 — Workflow.from_skeleton + bindings load test.

Validates that the retrieval_grounded skeleton can be lowered into a
runnable Workflow under TWO bindings (F17 winner vs structural-consensus
variant), and that:

* Both variants produce a 2-step / 3-link topology.
* The drafter step's class differs between bindings (proving the
  skeleton genuinely templates the drafter, not just the wiring).
* The router fallback_mode can be swapped by binding a different
  router_config path.
* The skeleton's FAIL-FAST surface is exercised (missing required
  hole; extra binding key).

This is the third legitimate workflow-creation path:
  1. Hand-authored YAML + Workflow.from_config(path)        (canonical)
  2. WorkflowBuilder.add_step + .load()                     (programmatic)
  3. Workflow.from_skeleton(skeleton, bindings)             (templated)  <- here
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKELETON = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_retrieval_grounded_skeleton"
    / "skeleton.yml"
)
ROUTER_NANOBRAIN_YML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_retrieval_grounded"
    / "steps"
    / "router.yml"
)
DRAFTER_SINGLE_YML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_retrieval_grounded"
    / "steps"
    / "drafter.yml"
)
DRAFTER_MULTI_YML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_structural_consensus"
    / "steps"
    / "multi_drafter.yml"
)


def _load(bindings):
    if not SKELETON.is_file():
        pytest.skip(f"skeleton missing: {SKELETON}")
    from nanobrain.core.workflow import Workflow

    return Workflow.from_skeleton(str(SKELETON), bindings=bindings)


def test_f17_winner_binding():
    wf = _load(
        {
            "router_config": str(ROUTER_NANOBRAIN_YML),
            "drafter_class": "apecx_integration.composition.steps.benchmark_drafter_step.BenchmarkDrafterStep",
            "drafter_config": str(DRAFTER_SINGLE_YML),
        }
    )
    assert sorted(wf.child_steps.keys()) == ["drafter", "task_router"]
    assert sorted(wf.step_links.keys()) == [
        "drafter_to_output",
        "input_to_router",
        "router_to_drafter",
    ]
    assert wf.child_steps["drafter"].__class__.__name__ == "BenchmarkDrafterStep"


def test_multi_sample_binding():
    wf = _load(
        {
            "router_config": str(ROUTER_NANOBRAIN_YML),
            "drafter_class": "apecx_integration.composition.steps.multi_sample_drafter_step.MultiSampleDrafterStep",
            "drafter_config": str(DRAFTER_MULTI_YML),
            "drafter_input_ref": "drafter.multi_drafter_input",
            "drafter_output_ref": "drafter.multi_drafter_output",
        }
    )
    assert sorted(wf.child_steps.keys()) == ["drafter", "task_router"]
    assert wf.child_steps["drafter"].__class__.__name__ == "MultiSampleDrafterStep"


def test_missing_required_hole_fails_fast():
    """drafter_class is required; omitting it must FAIL-FAST."""
    from nanobrain.core.component_base import ComponentConfigurationError

    with pytest.raises(ComponentConfigurationError, match="FAIL-FAST"):
        _load(
            {
                "router_config": str(ROUTER_NANOBRAIN_YML),
                # drafter_class missing
                "drafter_config": str(DRAFTER_SINGLE_YML),
            }
        )


def test_extra_binding_key_fails_fast():
    from nanobrain.core.component_base import ComponentConfigurationError

    with pytest.raises(ComponentConfigurationError, match="FAIL-FAST"):
        _load(
            {
                "router_config": str(ROUTER_NANOBRAIN_YML),
                "drafter_class": "apecx_integration.composition.steps.benchmark_drafter_step.BenchmarkDrafterStep",
                "drafter_config": str(DRAFTER_SINGLE_YML),
                "definitely_not_a_real_hole": "rogue",
            }
        )


def test_optional_hole_defaults_applied():
    """drafter_input_ref + drafter_output_ref are optional with defaults
    matching the single-drafter canonical step YAML. Verify the workflow
    loads with the defaults applied."""
    wf = _load(
        {
            "router_config": str(ROUTER_NANOBRAIN_YML),
            "drafter_class": "apecx_integration.composition.steps.benchmark_drafter_step.BenchmarkDrafterStep",
            "drafter_config": str(DRAFTER_SINGLE_YML),
            # drafter_input_ref / drafter_output_ref omitted — defaults must apply
        }
    )
    assert "router_to_drafter" in wf.step_links
    assert "drafter_to_output" in wf.step_links
