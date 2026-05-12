"""CW-GN1 — unit tests for the generic reflection skeleton.

Pins:
  1. Skeleton loads from disk via Workflow.from_skeleton.
  2. Binding to CodeWriteStep + CodeReviewStep produces a runnable
     Workflow with the right step + link counts.
  3. Missing required bindings → FAIL-FAST.
  4. Extra bindings (typos) → FAIL-FAST.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.core.component_base import ComponentConfigurationError
from nanobrain.core.workflow import Workflow

REPO_ROOT = Path(__file__).resolve().parents[2]
SKELETON_PATH = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "skeletons"
    / "generic"
    / "reflection_skeleton.yml"
)
WRAPPER_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing" / "steps"
)


_CODE_BINDINGS = {
    "generator_class": "apecx_integration.composition.steps.code_write_step.CodeWriteStep",
    "generator_config": str(WRAPPER_DIR / "code_write.yml"),
    "generator_input_du": "code_write_input",
    "generator_output_du": "code_write_output",
    "critic_class": "apecx_integration.composition.steps.code_review_step.CodeReviewStep",
    "critic_config": str(WRAPPER_DIR / "code_review.yml"),
    "critic_input_du": "code_review_input",
    "critic_output_du": "code_review_output",
}


def test_skeleton_file_exists():
    assert SKELETON_PATH.is_file(), SKELETON_PATH


def test_skeleton_binds_to_code_classes_and_loads():
    wf = Workflow.from_skeleton(str(SKELETON_PATH), bindings=_CODE_BINDINGS)
    assert wf.name == "generic_reflection_workflow"
    step_names = sorted(wf.child_steps.keys())
    assert step_names == ["critic", "generator"], step_names


def test_skeleton_missing_required_binding_fails_fast():
    bindings = dict(_CODE_BINDINGS)
    del bindings["generator_class"]
    with pytest.raises((ComponentConfigurationError, ValueError)) as exc:
        Workflow.from_skeleton(str(SKELETON_PATH), bindings=bindings)
    assert "generator_class" in str(exc.value) or "required" in str(exc.value).lower()


def test_skeleton_extra_binding_typo_fails_fast():
    bindings = dict(_CODE_BINDINGS)
    bindings["generator_classs"] = "x.Y.Z"  # typo
    with pytest.raises((ComponentConfigurationError, ValueError)) as exc:
        Workflow.from_skeleton(str(SKELETON_PATH), bindings=bindings)
    assert "generator_classs" in str(exc.value) or "extra" in str(exc.value).lower()


def test_skeleton_produces_workflow_with_expected_link_count():
    """The skeleton's body declares 4 DirectLinks; the lowered workflow
    must register all of them."""
    wf = Workflow.from_skeleton(str(SKELETON_PATH), bindings=_CODE_BINDINGS)
    links = getattr(wf, "step_links", None) or {}
    assert len(links) == 4, (
        f"expected 4 DirectLinks from the skeleton body, got {len(links)}: {list(links.keys())}"
    )
