"""Programmatic (lightweight) variant of code_reflection_workflow.yml.

Builds the same write → review topology via nanobrain's
``WorkflowBuilder``. Demonstrates the legit "second way" to author a
workflow that the workspace CLAUDE.md highlights:

  1. Hand-authored YAML + ``Workflow.from_config(path)``  — see
     ``workflows/code_writing/code_reflection_workflow.yml``.
  2. ``Workflow.from_skeleton(skeleton, bindings)`` — G9 typed
     bindings, deferred to a future iteration.
  3. **Lightweight programmatic** — this module. Best for code-
     generated workflows where the topology is parameterized by
     runtime values.

The programmatic path uses the dotted-path fallback in
``WorkflowBuilder.add_step`` (nanobrain framework expansion
2026-05-12) so the apecx-side step classes don't need to be
pre-registered in nanobrain's YAML-scan discovery.

Adopters who want a totally bespoke topology should call
``build_code_reflection_workflow`` for the canonical shape, then
mutate the returned ``WorkflowBuilder`` (it's stateful) before
calling ``.load()`` themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobrain.core.workflow import Workflow
from nanobrain.lightweight import WorkflowBuilder

CODE_WRITE_STEP_CLASS = "apecx_integration.composition.steps.code_write_step.CodeWriteStep"
CODE_REVIEW_STEP_CLASS = "apecx_integration.composition.steps.code_review_step.CodeReviewStep"

# The per-step wrapper YAMLs that already define the steps'
# input_data_units, output_data_units, and triggers. Reused here so
# the programmatic build matches the YAML build verbatim — operators
# who tune a step's prompt or temperature via the wrapper YAML
# automatically see the change in both authoring paths.
_REPO_CODE_WRITING_DIR = (
    Path(__file__).resolve().parents[2] / "composition" / "workflows" / "code_writing" / "steps"
)
_CODE_WRITE_WRAPPER = str(_REPO_CODE_WRITING_DIR / "code_write.yml")
_CODE_REVIEW_WRAPPER = str(_REPO_CODE_WRITING_DIR / "code_review.yml")


def build_code_reflection_workflow(
    *,
    workflow_name: str = "code_reflection_workflow_lightweight",
    description: str = "Generate-and-critique code reflection (programmatic build).",
    code_write_config: dict[str, Any] | None = None,
    code_review_config: dict[str, Any] | None = None,
) -> Workflow:
    """Build the code-reflection workflow programmatically.

    The topology mirrors ``code_reflection_workflow.yml``:
    ``workflow_input → code_write → code_review → outputs``.

    Args:
        workflow_name: Stable name for the constructed workflow. The
            default ends in ``_lightweight`` so an operator can tell
            at a glance whether they're looking at the YAML build or
            the programmatic one in logs / observability.
        description: Workflow-level description (surfaced in
            observability + manifest-equivalent listings).
        code_write_config: Optional dict of extra config fields for
            the CodeWriteStep (e.g., temperature, default_function_name).
            Merged into the step's inline config block.
        code_review_config: Same shape for CodeReviewStep.

    Returns:
        A loaded ``Workflow`` instance ready to ``await .run(...)``.
        The workflow's input data unit is ``workflow_input``; outputs
        are ``code_source``, ``function_name_verified``, and
        ``review_verdict`` (matching the YAML build).

    Raises:
        Whatever ``WorkflowBuilder.load`` raises — typically
        ``ComponentConfigurationError`` if a step class cannot be
        imported or its inline config is malformed.
    """
    builder = WorkflowBuilder(name=workflow_name, description=description)

    # Both steps reference the bundled per-step wrapper YAMLs for
    # their input/output data units + triggers. Lightweight builds
    # CAN inline the full step config, but reusing the wrappers keeps
    # the YAML and programmatic paths bit-identical in behavior.
    write_step_extras: dict[str, Any] = {
        "description": "LLM-backed Python authoring.",
        "config": _CODE_WRITE_WRAPPER,
    }
    if code_write_config:
        write_step_extras.update(code_write_config)
    builder.add_step("code_write", CODE_WRITE_STEP_CLASS, **write_step_extras)

    review_step_extras: dict[str, Any] = {
        "description": "Structured critique of generated code.",
        "config": _CODE_REVIEW_WRAPPER,
    }
    if code_review_config:
        review_step_extras.update(code_review_config)
    builder.add_step("code_review", CODE_REVIEW_STEP_CLASS, **review_step_extras)

    builder.add_input("workflow_input")
    # Mirrors the 2026-05-12 YAML refactor: ONE workflow-level output
    # ("reflection_result"). Three links total (was 5 with parallel
    # links to multiple outputs). Single-output design lets the
    # SubworkflowStep single-output flatten apply when this workflow
    # is embedded in a parent.
    builder.add_output("reflection_result")

    builder.add_link(
        "workflow_input",
        "code_write.code_write_input",
        link_type="direct",
        auto_transfer=True,
    )
    builder.add_link(
        "code_write.code_write_output",
        "code_review.code_review_input",
        link_type="direct",
        auto_transfer=True,
    )
    builder.add_link(
        "code_review.code_review_output",
        "reflection_result",
        link_type="direct",
        auto_transfer=True,
    )

    builder.add_trigger(step_name="code_write", trigger_type="data_updated")
    builder.add_trigger(step_name="code_review", trigger_type="data_updated")

    # As of nanobrain 2026-05-15 the lightweight builder emits the
    # nested-shape link entries the framework's LinkBase.from_config
    # expects, so ``builder.load()`` is the one-line happy path. The
    # earlier ``_rewrap_link_entries_nested`` + ``_materialize_config_as_yaml``
    # workaround for friction-log #26 has been retired now that
    # ``WorkflowBuilder.add_link`` is fixed at the source.
    return builder.load()


__all__ = ["build_code_reflection_workflow"]
