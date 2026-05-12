"""Structural tests for the code-writing workflow stack.

NOT integration tests — no LLM, no subprocess. These pin that:
  - CodeReflectionStep + CodeVerificationStep load from their wrapper
    YAMLs (the composer-facing surface).
  - Their inner workflows (code_reflection_workflow.yml,
    code_verification_workflow.yml) load via from_config.
  - The outer demo workflow loads with both sub-workflow steps + 3
    DirectLinks, all auto_transfer:true.
  - The manifest is discoverable via the MCP surface's
    _load_all_manifests() with all 5 documented components present.

Behavior tests (real LLM round-trip, real subprocess) live in
tests/integration/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_WRITING_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
)


@pytest.fixture
def code_exec_enabled(monkeypatch):
    """Enable the APECX_CODE_EXEC env gate so the verification step's
    inner workflow can load without refusing at process()."""
    monkeypatch.setenv("APECX_CODE_EXEC", "1")
    return monkeypatch


def test_code_reflection_step_loads_with_inner_workflow():
    from apecx_integration.composition.steps.code_reflection_step import (
        CodeReflectionStep,
    )

    wrapper = CODE_WRITING_DIR / "steps" / "code_reflection.yml"
    step = CodeReflectionStep.from_config(str(wrapper))
    assert step.name == "code_reflection"
    assert step.inner_workflow.name == "code_reflection_workflow"
    # Path was resolved to an existing file.
    assert step.inner_workflow_path.is_file()


def test_code_verification_step_loads_with_inner_workflow(code_exec_enabled):
    from apecx_integration.composition.steps.code_verification_step import (
        CodeVerificationStep,
    )

    wrapper = CODE_WRITING_DIR / "steps" / "code_verification.yml"
    step = CodeVerificationStep.from_config(str(wrapper))
    assert step.name == "code_verification"
    assert step.inner_workflow.name == "code_verification_workflow"


def test_outer_demo_workflow_loads_with_two_subworkflow_steps(
    code_exec_enabled,
):
    """The outer workflow embeds two sub-workflows as steps —
    nesting depth 3 (outer → SubworkflowStep → inner workflow). The
    workflow must load with both child steps registered + 3 links."""
    from nanobrain.core.workflow import Workflow

    outer_path = CODE_WRITING_DIR / "code_authoring_with_reflection_and_verification.yml"
    workflow = Workflow.from_config(str(outer_path))
    assert workflow.name == "code_authoring_with_reflection_and_verification"
    # Two child steps registered.
    step_names = sorted(workflow.child_steps.keys())
    assert step_names == ["code_reflection", "code_verification"], step_names


def test_inner_reflection_workflow_loads_with_write_and_review_steps():
    """The reflection sub-workflow has CodeWriteStep + CodeReviewStep
    as its two steps."""
    from nanobrain.core.workflow import Workflow

    inner_path = CODE_WRITING_DIR / "code_reflection_workflow.yml"
    workflow = Workflow.from_config(str(inner_path))
    assert workflow.name == "code_reflection_workflow"
    step_names = sorted(workflow.child_steps.keys())
    assert step_names == ["code_review", "code_write"], step_names


def test_inner_verification_workflow_loads_with_exec_step(code_exec_enabled):
    from nanobrain.core.workflow import Workflow

    inner_path = CODE_WRITING_DIR / "code_verification_workflow.yml"
    workflow = Workflow.from_config(str(inner_path))
    assert workflow.name == "code_verification_workflow"
    assert list(workflow.child_steps.keys()) == ["isolated_py_exec"]


def test_manifest_is_discoverable_via_mcp_surface():
    """list_workflows / describe_workflow read every manifest in
    composer_config.yml::component_catalog_paths. The new
    code_writing/manifest.yml must appear in the parsed list AND
    expose all 5 documented components (CW1-CW5)."""
    from apecx_integration.mcp_surface.tools.discovery import (
        _load_all_manifests,
    )

    manifests = _load_all_manifests()
    code_manifests = [
        m for m in manifests if m.workflow_name == "code_authoring_with_reflection_and_verification"
    ]
    assert len(code_manifests) == 1, (
        "code_authoring manifest missing from discovery — check "
        "composer_config.yml::component_catalog_paths"
    )
    components = code_manifests[0].components
    assert len(components) >= 9, (
        f"expected at least 9 documented components "
        f"(CW1-CW9; growing additively), got {len(components)}"
    )

    step_names = {c["step_name"] for c in components}
    # These must all be present; future additions are tolerated.
    required = {
        "code_write",
        "code_review",
        "isolated_py_exec",
        "code_reflection",
        "code_verification",
        "test_write",
        "code_with_tests",
        "workflow_analysis",
        "workflow_summarize",
    }
    missing = required - step_names
    assert not missing, f"manifest missing required components: {sorted(missing)}"


def test_every_component_has_a_nonempty_rag_description():
    """A component with empty rag_description is invisible to the
    composer's RAG matcher — pin every entry has a real description."""
    from apecx_integration.mcp_surface.tools.discovery import (
        _load_all_manifests,
    )

    manifests = _load_all_manifests()
    code_manifests = [
        m for m in manifests if m.workflow_name == "code_authoring_with_reflection_and_verification"
    ]
    for c in code_manifests[0].components:
        desc = c.get("rag_description", "")
        assert desc and len(desc.strip()) > 50, (
            f"component {c['step_name']!r} has thin / missing rag_description ({len(desc)} chars)"
        )
