"""LW — bridge from nanobrain.lightweight.WorkflowBuilder to the
A1 framework-rule validator.

The lightweight workflow-authoring path (``nanobrain.lightweight``)
is a legit third way to build workflows alongside hand-authored YAML
and the LLM composer. Without this bridge, programmatic builders
got the framework's runtime errors but NOT A1's structured-
violation surface — defeating the point of the loop closure.

This module exposes two helpers:

- ``validate_lightweight_builder(builder)`` — runs the validator
  over the builder's ``get_config()`` dict. Returns the same
  ``WorkflowViolation`` tuple as the composer path. Caller decides
  whether to raise / log / retry.
- ``validate_and_load(builder)`` — convenience for the common
  "validate then call builder.load()" pattern. Raises
  ``WorkflowValidationError`` on any violation; otherwise returns
  the loaded ``Workflow`` instance.

Why a separate module rather than methods on the validator
---------------------------------------------------------
``workflow_validator.py`` keeps a clean dependency surface: it
imports from ``_errors`` and from nanobrain core. Pulling in the
``nanobrain.lightweight.workflow_builder`` import here would force
the heavier lightweight stack onto every composer-side import.
Operators who don't use the LW path pay nothing.
"""

from __future__ import annotations

from typing import Any

from apecx_integration.composition.workflow_validator import (
    WorkflowValidationError,
    WorkflowViolation,
    validate_workflow_against_framework,
)


def validate_lightweight_builder(
    builder: Any,
    *,
    catalog_class_paths: set[str] | None = None,
) -> tuple[WorkflowViolation, ...]:
    """Run the A1 validator over a WorkflowBuilder's current config.

    Args:
        builder: a ``nanobrain.lightweight.workflow_builder.WorkflowBuilder``
            instance (or anything with a ``get_config()`` method
            returning the workflow dict). We duck-type so a future
            ``EnhancedWorkflowBuilder`` doesn't need a special case.
        catalog_class_paths: optional set of catalog class paths.
            When provided, CPR's "did you mean X?" hint augments the
            ``step_class_unresolvable`` violation's suggested_fix.
            Lightweight callers typically know their component
            universe at build time; passing it gives them parity
            with the composer's hint surface.

    Returns:
        The same violations tuple ``validate_workflow_against_framework``
        produces — empty when the workflow is framework-legal.
    """
    if not hasattr(builder, "get_config"):
        raise TypeError(
            "validate_lightweight_builder expected a builder with a "
            f"get_config() method; got {type(builder).__name__}"
        )
    config = builder.get_config()
    if not isinstance(config, dict):
        raise TypeError(f"builder.get_config() must return a dict; got {type(config).__name__}")
    return validate_workflow_against_framework(config, catalog_class_paths=catalog_class_paths)


def validate_and_load(builder: Any):
    """Validate the builder's config, then call ``builder.load()``.

    Raises ``WorkflowValidationError`` with structured violations
    before the heavy ``Workflow.from_config`` call — same retry
    surface as the LLM composer pipeline, but for programmatic
    workflow construction.

    Returns the loaded ``Workflow`` instance on success.
    """
    violations = validate_lightweight_builder(builder)
    if violations:
        import yaml as _yaml

        try:
            yaml_text = _yaml.safe_dump(builder.get_config())
        except Exception:
            yaml_text = None
        raise WorkflowValidationError(
            violations=violations,
            yaml_text=yaml_text,
        )
    return builder.load()


def repair_lightweight_builder_class_paths(
    builder: Any,
    catalog_class_paths: set[str],
) -> list:
    """Apply CPR (2026-05-11) auto-repairs to a builder's config.

    Mirrors the composer's pre-validate repair pass for the
    lightweight path. Mutates ``builder.get_config()`` in place;
    returns the list of repairs applied. Callers that want the
    repair list persisted should record them themselves — this
    helper does not have a CompositionSummary to thread through.

    Note: ``WorkflowBuilder.get_config()`` returns a fresh copy on
    each call in the upstream implementation. To get an in-place
    repair, callers may need to use ``builder.workflow_config``
    directly (the underlying mutable dict). We try both: if
    get_config returns a dict that's the same object as
    workflow_config, mutating it works; otherwise mutate
    workflow_config directly when available.
    """
    from apecx_integration.composition.class_path_resolver import (
        repair_workflow_class_paths,
    )

    # Prefer the underlying mutable attribute when present —
    # WorkflowBuilder exposes workflow_config as the canonical
    # store, and get_config returns a copy.
    config = getattr(builder, "workflow_config", None)
    if not isinstance(config, dict):
        config = builder.get_config()
    return repair_workflow_class_paths(config, catalog_class_paths)


__all__ = [
    "repair_lightweight_builder_class_paths",
    "validate_and_load",
    "validate_lightweight_builder",
]
