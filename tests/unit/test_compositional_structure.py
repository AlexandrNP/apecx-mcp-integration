"""Compositional structural tests for the shipped workflow pyramid.

No LLM, no subprocess. Pure structural pins to prevent silent
regressions in the workflow YAML wiring across the shipped
compositional patterns:

  - code_reflection_workflow.yml (inner)
  - code_verification_workflow.yml (inner)
  - code_with_tests_workflow.yml (write→tests)
  - code_authoring_with_reflection_and_verification.yml (outer; nested)
  - self_improving_code_writing.yml (4-step flat with memory)
  - generic/reflection_skeleton.yml (G9 bindings)

Each pin asserts: file present, loads cleanly, child_steps as
documented, step_links count, all DirectLinks have auto_transfer:true.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
SKELETONS = REPO_ROOT / "src" / "apecx_integration" / "composition" / "skeletons"


def _load_workflow(rel_path: str):
    from nanobrain.core.workflow import Workflow

    return Workflow.from_config(str(WORKFLOWS / rel_path))


@pytest.fixture
def code_exec_enabled(monkeypatch):
    monkeypatch.setenv("APECX_CODE_EXEC", "1")
    return monkeypatch


# ---------------------------------------------------------------------------
# Inner workflows
# ---------------------------------------------------------------------------


def test_code_reflection_workflow_topology():
    wf = _load_workflow("code_reflection_workflow.yml")
    assert wf.name == "code_reflection_workflow"
    assert sorted(wf.child_steps.keys()) == ["code_review", "code_write"]
    # 3 links after the single-output refactor:
    # input → write, write → review, review → reflection_result
    assert len(wf.step_links) == 3


def test_code_verification_workflow_topology(code_exec_enabled):
    wf = _load_workflow("code_verification_workflow.yml")
    assert wf.name == "code_verification_workflow"
    assert list(wf.child_steps.keys()) == ["isolated_py_exec"]
    assert len(wf.step_links) == 2


def test_code_with_tests_workflow_topology():
    wf = _load_workflow("code_with_tests_workflow.yml")
    assert wf.name == "code_with_tests_workflow"
    assert sorted(wf.child_steps.keys()) == ["code_write", "test_write"]
    assert len(wf.step_links) == 5


# ---------------------------------------------------------------------------
# Outer / self-improving workflows
# ---------------------------------------------------------------------------


def test_outer_demo_workflow_topology(code_exec_enabled):
    wf = _load_workflow("code_authoring_with_reflection_and_verification.yml")
    assert wf.name == "code_authoring_with_reflection_and_verification"
    assert sorted(wf.child_steps.keys()) == [
        "code_reflection",
        "code_verification",
    ]
    assert len(wf.step_links) == 3


def test_self_improving_workflow_topology():
    wf = _load_workflow("self_improving_code_writing.yml")
    assert wf.name == "self_improving_code_writing_workflow"
    assert sorted(wf.child_steps.keys()) == [
        "code_review",
        "code_write",
        "memory_read",
        "memory_write",
    ]
    assert len(wf.step_links) == 7


# ---------------------------------------------------------------------------
# Cross-pattern: every shipped workflow's DirectLinks all have auto_transfer:true
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path,needs_exec",
    [
        ("code_reflection_workflow.yml", False),
        ("code_with_tests_workflow.yml", False),
        ("self_improving_code_writing.yml", False),
        ("code_verification_workflow.yml", True),
        ("code_authoring_with_reflection_and_verification.yml", True),
    ],
)
def test_all_directlinks_have_auto_transfer_true(
    rel_path, needs_exec, code_exec_enabled, monkeypatch
):
    """G7 silent-failure pin per workspace CLAUDE.md. Every DirectLink
    in every shipped workflow must declare auto_transfer:true (or be
    auto-injected by the v2 mutator on load). After load, the
    LinkBase instances expose auto_transfer; pin it == True."""
    if not needs_exec:
        # Don't need APECX_CODE_EXEC; reset for purity.
        monkeypatch.delenv("APECX_CODE_EXEC", raising=False)
    wf = _load_workflow(rel_path)
    direct_links = [
        link for link in (wf.step_links or {}).values() if "DirectLink" in type(link).__name__
    ]
    assert direct_links, f"no DirectLinks found in {rel_path}"
    for link in direct_links:
        assert getattr(link, "auto_transfer", False) is True, (
            f"DirectLink {link} in {rel_path} has auto_transfer != True; "
            f"silent-failure risk per CLAUDE.md"
        )


# ---------------------------------------------------------------------------
# Generic reflection skeleton — G9 cross-domain
# ---------------------------------------------------------------------------


def test_generic_reflection_skeleton_binds_for_code_pair():
    """The shipped skeleton at skeletons/generic/reflection_skeleton.yml
    must produce a runnable Workflow when bound to CodeWriteStep +
    CodeReviewStep. This is the cross-domain reuse demo."""
    from nanobrain.core.workflow import Workflow

    skeleton_path = SKELETONS / "generic" / "reflection_skeleton.yml"
    wrappers = WORKFLOWS / "steps"
    bindings = {
        "generator_class": ("apecx_integration.composition.steps.code_write_step.CodeWriteStep"),
        "generator_config": str(wrappers / "code_write.yml"),
        "generator_input_du": "code_write_input",
        "generator_output_du": "code_write_output",
        "critic_class": ("apecx_integration.composition.steps.code_review_step.CodeReviewStep"),
        "critic_config": str(wrappers / "code_review.yml"),
        "critic_input_du": "code_review_input",
        "critic_output_du": "code_review_output",
    }
    wf = Workflow.from_skeleton(str(skeleton_path), bindings=bindings)
    assert wf.name == "generic_reflection_workflow"
    assert sorted(wf.child_steps.keys()) == ["critic", "generator"]
    # 4 DirectLinks in the skeleton body.
    assert len(wf.step_links) == 4
