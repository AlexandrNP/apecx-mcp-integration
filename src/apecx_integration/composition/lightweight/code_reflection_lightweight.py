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

    # The lightweight builder emits each link entry in FLAT shape:
    #   link_0: {class, source, target, auto_transfer}
    # The framework's link loader (LinkBase.from_config) expects the
    # NESTED shape used by hand-authored YAML:
    #   link_0: {class, config: {source, target, auto_transfer, ...}}
    # We rewrap here so the loaded Workflow gets functional links.
    # When the framework's lightweight builder catches up to the
    # nested shape this transform becomes a no-op (the same keys).
    cfg = builder.get_config()
    cfg["links"] = _rewrap_link_entries_nested(cfg.get("links") or {})
    return Workflow.from_config(_materialize_config_as_yaml(cfg))


def _rewrap_link_entries_nested(
    links_flat: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Convert flat-shape link entries to nested config-keyed shape."""
    rewrapped: dict[str, dict[str, Any]] = {}
    for name, entry in links_flat.items():
        if not isinstance(entry, dict):
            rewrapped[name] = entry
            continue
        if "config" in entry:
            rewrapped[name] = entry
            continue
        cls = entry.get("class")
        nested_config = {k: v for k, v in entry.items() if k not in ("class", "name")}
        nested_config.setdefault("link_type", "direct")
        rewrapped[name] = {"name": name, "class": cls, "config": nested_config}
    return rewrapped


def _materialize_config_as_yaml(cfg: dict[str, Any]) -> str:
    """Write ``cfg`` to a temp YAML file; return the path string."""
    import contextlib
    import os
    import tempfile

    import yaml

    fd, path = tempfile.mkstemp(suffix=".yml", prefix="apecx_lightweight_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    # NOTE: the file persists on disk after this call returns. The
    # caller (Workflow.from_config) reads it synchronously; the
    # file's content is immutable thereafter, so leaving it for the
    # OS to clean up in /tmp on reboot is fine — the alternative
    # (delete after load) would race the Workflow's own re-resolve
    # paths if the loader keeps the path for resolving relative
    # references.
    return path


__all__ = ["build_code_reflection_workflow"]
