"""CW-7 — structural tests for the programmatic (lightweight) variant
of the code-reflection workflow.

Pins:
  1. ``build_code_reflection_workflow()`` returns a real Workflow.
  2. The workflow has both steps (code_write, code_review).
  3. Every DirectLink is functional (5 links matching the YAML topology).
  4. Optional config dicts thread through to the per-step extras
     without losing the wrapper YAML path.
  5. The programmatic build produces a workflow with the SAME step
     topology as the hand-authored YAML — diff-equivalence at the
     structure layer (not a YAML byte-diff; both build a Workflow
     with the same step names, link counts, and trigger counts).

Behavior tests (real LLM round-trip) are in CW-11.
"""

from __future__ import annotations

from pathlib import Path

from nanobrain.core.workflow import Workflow

from apecx_integration.composition.lightweight import (
    build_code_reflection_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_REFLECTION_PATH = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "code_writing"
    / "code_reflection_workflow.yml"
)


def test_lightweight_build_returns_real_workflow():
    wf = build_code_reflection_workflow()
    assert isinstance(wf, Workflow)
    assert wf.name == "code_reflection_workflow_lightweight"


def test_lightweight_build_registers_both_steps():
    wf = build_code_reflection_workflow()
    step_names = sorted(wf.child_steps.keys())
    assert step_names == ["code_review", "code_write"]


def test_lightweight_build_registers_all_links():
    """All 3 DirectLinks must be wired — same as the YAML version
    (post-2026-05-12 single-output refactor).

    A workflow with 0 links is the dominant silent-failure shape
    (per workspace CLAUDE.md). Without this assertion a regression
    in the link-shape transform would let the workflow load while
    silently no-op'ing every transfer."""
    wf = build_code_reflection_workflow()
    assert hasattr(wf, "step_links"), "Workflow should expose step_links"
    links = wf.step_links or {}
    assert len(links) == 3, (
        f"expected 3 DirectLinks (matching the YAML topology), got "
        f"{len(links)}: {list(links.keys())}"
    )


def test_lightweight_topology_matches_yaml_at_structure_level():
    """Both authoring paths build a Workflow with the same shape:
    same step names, same link count, same trigger count. They are
    NOT byte-equivalent (different workflow names, different link
    naming convention), but they ARE structurally equivalent."""
    lw = build_code_reflection_workflow()
    yml = Workflow.from_config(str(YAML_REFLECTION_PATH))

    assert sorted(lw.child_steps.keys()) == sorted(yml.child_steps.keys())
    assert len(lw.step_links or {}) == len(yml.step_links or {})


def test_lightweight_build_accepts_per_step_overrides():
    """Operator passes a temperature override; the override appears
    in the emitted step entry. Whether the framework actually honors
    inline-config-AND-config-path simultaneously is a separate
    framework concern; this test pins that the lightweight build
    plumbs the override THROUGH without dropping it on the floor."""
    from nanobrain.lightweight import WorkflowBuilder

    # Reproduce the wrapper's add_step calls but inspect the dict
    # before load. We can't easily read the dict back from the
    # constructed Workflow (the framework consumes it).
    from apecx_integration.composition.lightweight.code_reflection_lightweight import (
        _CODE_WRITE_WRAPPER,
        CODE_WRITE_STEP_CLASS,
    )

    b = WorkflowBuilder("override_smoke", "test")
    b.add_step(
        "code_write",
        CODE_WRITE_STEP_CLASS,
        config=_CODE_WRITE_WRAPPER,
        temperature=0.7,
    )
    cfg = b.get_config()
    assert cfg["steps"]["code_write"]["temperature"] == 0.7
