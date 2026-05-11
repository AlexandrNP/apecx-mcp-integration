"""Framework-native pre-execution validator for composed workflow YAML.

Runs between ``yaml.safe_load(...)`` and ``Workflow.from_config(...)`` in
the composer pipeline so framework-illegal workflows are rejected at
compose-time with a structured, LLM-actionable error — not at executor
time as an opaque ``ValueError`` traceback.

Authoritative framework rules consulted, by reference rather than
re-implementation:

- Inline-dict vs. file-path config: ``nanobrain.core.config.config_base
  .ConfigBase._is_inline_config_supported`` (config_base.py:1182).
  Only ``DataUnit / Link / Trigger`` subclasses accept inline dict.
- ``DirectLink`` ``auto_transfer`` semantics: ``nanobrain.core.link``.
  Framework default is now True (G7 Step 5, 2026-05-09), but the
  composer's system prompt still requires explicit ``auto_transfer:
  true`` for defense-in-depth; we surface explicit ``false`` as a
  violation because that re-opens the dominant silent-failure shape.
- ``TransformLink`` ban: composer system.md rule (LLMs hallucinate
  ``transform_function`` paths). Use DirectLink + novel Python instead.

The validator returns a list of ``WorkflowViolation`` records. The
composer wraps a non-empty list into ``WorkflowValidationError`` whose
``to_feedback_payload()`` formats violations as an assistant-correction
turn for the C1 retry loop.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from apecx_integration.composition._errors import ComposerResponseError

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class WorkflowViolation:
    """A single framework-rule violation in a composed workflow.

    ``rule_id`` is stable for telemetry / retry-loop pattern matching;
    don't rename without grepping callers. ``path`` is a YAML-pointer-
    style string for the offending location (e.g. ``steps.foo.config``);
    intentionally human-readable rather than RFC-6901 so the message
    pasted back to the LLM is readable in plain text.
    """

    rule_id: str
    path: str
    message: str
    suggested_fix: str


class WorkflowValidationError(ComposerResponseError):
    """The LLM emitted a workflow that violates framework rules.

    Subclasses ``ComposerResponseError`` so existing callers that
    treat "LLM output is bad" uniformly continue to work. New callers
    that want the structured violation payload check
    ``isinstance(exc, WorkflowValidationError)`` then read
    ``exc.violations``.
    """

    def __init__(
        self,
        violations: tuple[WorkflowViolation, ...],
        *,
        yaml_text: str | None = None,
    ) -> None:
        self.violations = violations
        self.yaml_text = yaml_text
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        header = (
            f"Composed workflow failed framework-rule validation "
            f"({len(self.violations)} violation"
            f"{'s' if len(self.violations) != 1 else ''}):"
        )
        lines = [header]
        for v in self.violations:
            lines.append(f"  [{v.rule_id}] {v.path}")
            lines.append(f"      {v.message}")
            lines.append(f"      fix: {v.suggested_fix}")
        return "\n".join(lines)

    def to_feedback_payload(self) -> str:
        """Format violations as an LLM-facing correction prompt.

        Used by the C1 retry loop: the prior YAML response is sent
        back as an assistant turn, then this string is appended as a
        user turn so the LLM sees exactly what to repair.
        """
        if not self.violations:
            return "(no violations)"
        lines = [
            "Your previous workflow YAML failed framework validation. "
            "Each violation lists the rule, the YAML location, and the "
            "exact fix. Emit a corrected workflow YAML — preserve "
            "everything that did NOT violate a rule.",
            "",
        ]
        for i, v in enumerate(self.violations, start=1):
            lines.append(f"{i}. [{v.rule_id}] at `{v.path}`")
            lines.append(f"   problem: {v.message}")
            lines.append(f"   fix:     {v.suggested_fix}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _import_class(class_path: str) -> tuple[type | None, str | None]:
    """Best-effort import of a dotted class path.

    Returns ``(class_obj, None)`` on success, ``(None, reason)`` on
    failure. We deliberately swallow every exception class importlib /
    getattr can raise — the validator must not crash on malformed
    class paths; that's the LLM's bug to surface, not ours.
    """
    if not class_path or "." not in class_path:
        return None, "class path is missing or has no module component"
    module_path, _, class_name = class_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # ImportError, ValueError, ModuleNotFoundError
        return None, f"could not import module {module_path!r}: {exc!s}"
    target = getattr(module, class_name, None)
    if target is None:
        return None, (f"module {module_path!r} has no attribute {class_name!r}")
    if not isinstance(target, type):
        return None, (f"{class_path!r} resolves to a {type(target).__name__}, not a class")
    return target, None


def _is_inline_config_supported(target_class: type) -> bool:
    """Delegate to the framework's authoritative classifier.

    Imports lazily so the validator can be unit-tested without the
    full nanobrain stack on the path — if the framework is missing we
    fall back to a conservative ``False`` (treat all classes as
    file-path-only), which matches the framework's own fallback in
    ``_is_inline_config_supported`` at ``config_base.py:1207``.
    """
    try:
        from nanobrain.core.config.config_base import ConfigBase
    except ImportError:
        return False
    return ConfigBase._is_inline_config_supported(target_class)


def _is_step_subclass(target_class: type) -> bool:
    """True iff ``target_class`` is a ``BaseStep`` (or alias) subclass."""
    try:
        from nanobrain.core.step import BaseStep
    except ImportError:
        return False
    try:
        return issubclass(target_class, BaseStep)
    except TypeError:
        return False


def _is_link_subclass(target_class: type) -> bool:
    try:
        from nanobrain.core.link import LinkBase
    except ImportError:
        return False
    try:
        return issubclass(target_class, LinkBase)
    except TypeError:
        return False


def _ref_target_is_workflow_level(ref: str, workflow_dict: dict[str, Any]) -> bool:
    """True iff ``ref`` is a bare name matching a workflow-level data unit."""
    if "." in ref:
        return False
    return any(
        ref in (workflow_dict.get(block) or {})
        for block in ("input_data_units", "output_data_units")
    )


def _ref_target_is_step_qualified(ref: str, workflow_dict: dict[str, Any]) -> bool:
    """True iff ``ref`` is ``<step_id>.<du_name>`` with a known step_id.

    We deliberately do NOT check that ``<du_name>`` exists on the
    step's wrapper YAML. Doing so would require loading every step
    config from disk to peek at its ``input_data_units`` /
    ``output_data_units`` blocks — a 10x cost on a fast-path validator.
    The framework catches dangling du-names at workflow-init via
    ``WorkflowGraph`` orphan-detection (cycle/orphan validation in
    ``nanobrain/core/workflow_graph.py``) with a precise error;
    duplicating that here would only add a second source of truth.
    """
    if "." not in ref:
        return False
    step_id, _, _du_name = ref.partition(".")
    return step_id in (workflow_dict.get("steps") or {})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_workflow_against_framework(
    workflow_dict: dict[str, Any],
    *,
    catalog_yaml_paths: dict[str, str] | None = None,
) -> tuple[WorkflowViolation, ...]:
    """Run the framework-rule pass over a parsed composed workflow.

    Args:
        workflow_dict: top-level mapping produced by ``yaml.safe_load``.
        catalog_yaml_paths: optional class_path → wrapper YAML path
            map from the composer's retrieval hits. When provided,
            string-form step configs that don't match the catalog's
            canonical path get a soft violation
            (``step_config_non_canonical_path``) — the reviewer can
            confirm or correct. When None, the canonical-path check
            is skipped.

    Returns:
        Tuple of violations, empty when the workflow is framework-legal.
        Order is stable across runs (preserves discovery order) so
        downstream snapshot tests don't flap.
    """
    violations: list[WorkflowViolation] = []

    if not isinstance(workflow_dict, dict):
        violations.append(
            WorkflowViolation(
                rule_id="workflow_not_mapping",
                path="$",
                message=(f"top-level YAML must be a mapping; got {type(workflow_dict).__name__}"),
                suggested_fix=(
                    "Emit a YAML document whose root is a mapping with "
                    "keys like `name:`, `steps:`, `links:`."
                ),
            )
        )
        return tuple(violations)

    violations.extend(_validate_steps_block(workflow_dict, catalog_yaml_paths))
    violations.extend(_validate_links_block(workflow_dict))
    return tuple(violations)


def _validate_steps_block(
    workflow_dict: dict[str, Any],
    catalog_yaml_paths: dict[str, str] | None,
) -> list[WorkflowViolation]:
    violations: list[WorkflowViolation] = []
    steps = workflow_dict.get("steps")
    if steps is None:
        # A workflow with no steps is degenerate but not necessarily
        # framework-illegal; the framework will surface a different
        # error at init time. Don't add a violation here.
        return violations
    if not isinstance(steps, dict):
        violations.append(
            WorkflowViolation(
                rule_id="steps_block_not_mapping",
                path="steps",
                message=(f"`steps:` must be a mapping; got {type(steps).__name__}"),
                suggested_fix=(
                    "Emit `steps:` as a mapping of `<step_id>: { class: "
                    "..., config: ... }` entries."
                ),
            )
        )
        return violations

    for step_id, body in steps.items():
        prefix = f"steps.{step_id}"
        if not isinstance(body, dict):
            violations.append(
                WorkflowViolation(
                    rule_id="step_body_not_mapping",
                    path=prefix,
                    message=(
                        f"step body must be a mapping with `class:` and "
                        f"`config:` keys; got {type(body).__name__}"
                    ),
                    suggested_fix=(f'Use `{step_id}: {{ class: "...", config: "..." }}`.'),
                )
            )
            continue

        class_path = body.get("class")
        config_value = body.get("config")

        if not class_path:
            violations.append(
                WorkflowViolation(
                    rule_id="step_class_missing",
                    path=f"{prefix}.class",
                    message="step has no `class:` field.",
                    suggested_fix=(
                        "Set `class:` to a fully-qualified Step subclass "
                        "path (e.g. `apecx_integration.composition.steps."
                        "synthesis_context_assembly_step."
                        "SynthesisContextAssemblyStep`)."
                    ),
                )
            )
            continue

        target_class, import_err = _import_class(str(class_path))
        if target_class is None:
            violations.append(
                WorkflowViolation(
                    rule_id="step_class_unresolvable",
                    path=f"{prefix}.class",
                    message=(
                        f"class path {class_path!r} could not be imported "
                        f"({import_err}). The composer likely hallucinated "
                        "this path."
                    ),
                    suggested_fix=(
                        "Pick a class path from the retrieved candidates "
                        "block (the prompt lists them verbatim). Do NOT "
                        "invent class paths."
                    ),
                )
            )
            # Without the class, the inline-dict check is also impossible
            continue

        if not _is_step_subclass(target_class):
            violations.append(
                WorkflowViolation(
                    rule_id="step_class_not_step_subclass",
                    path=f"{prefix}.class",
                    message=(
                        f"{class_path!r} is not a subclass of "
                        "`nanobrain.core.step.BaseStep`; only Step "
                        "subclasses belong under `steps:`."
                    ),
                    suggested_fix=(
                        "Move this entry out of `steps:` or pick a Step "
                        "subclass from the retrieved candidates."
                    ),
                )
            )
            # Continue checking config shape — useful to surface multiple
            # issues per step in one feedback round.

        if isinstance(config_value, dict):
            if not _is_inline_config_supported(target_class):
                class_name = target_class.__name__ if target_class else "Step"
                violations.append(
                    WorkflowViolation(
                        rule_id="step_inline_config_forbidden",
                        path=f"{prefix}.config",
                        message=(
                            f"inline dict `config:` is forbidden for "
                            f"{class_path}. The framework rule "
                            "(`ConfigBase._is_inline_config_supported`) "
                            "permits inline dict ONLY for DataUnit / Link "
                            "/ Trigger subclasses; everything else must "
                            "reference a wrapper YAML by path."
                        ),
                        suggested_fix=(
                            f'Replace with `config: "steps/'
                            f'{class_name.lower()}.yml"` (use the exact '
                            "path from the retrieved candidates "
                            "`(config: ...)` line). The wrapper YAML "
                            "already declares `input_data_units`, "
                            "`output_data_units`, and `triggers` — do "
                            "not duplicate them inline."
                        ),
                    )
                )
        elif isinstance(config_value, str):
            if catalog_yaml_paths is not None and target_class is not None:
                canonical = catalog_yaml_paths.get(str(class_path))
                if canonical is not None and config_value != canonical:
                    violations.append(
                        WorkflowViolation(
                            rule_id="step_config_non_canonical_path",
                            path=f"{prefix}.config",
                            message=(
                                f"`config:` is {config_value!r}; the "
                                f"retrieved component lists "
                                f"`config: {canonical!r}`. A non-canonical "
                                "path either points at a bespoke wrapper "
                                "(intended) or is a hallucinated typo "
                                "(unintended). The validator cannot tell "
                                "which without a filesystem check, so this "
                                "is a soft violation."
                            ),
                            suggested_fix=(
                                f"If the canonical wrapper fits, use "
                                f'`config: "{canonical}"`. Otherwise '
                                "confirm the bespoke path exists in the "
                                "workflow staging directory."
                            ),
                        )
                    )
        elif config_value is None:
            # Some steps may legitimately accept None (rare). Don't
            # raise here; the framework's from_config will surface a
            # precise error if config is required.
            pass
        else:
            violations.append(
                WorkflowViolation(
                    rule_id="step_config_wrong_type",
                    path=f"{prefix}.config",
                    message=(
                        f"`config:` must be a string path or an inline "
                        f"mapping (for DataUnit/Link/Trigger only); "
                        f"got {type(config_value).__name__}"
                    ),
                    suggested_fix=('Use a string path like `config: "steps/foo.yml"`.'),
                )
            )

    return violations


def _validate_links_block(
    workflow_dict: dict[str, Any],
) -> list[WorkflowViolation]:
    violations: list[WorkflowViolation] = []
    links = workflow_dict.get("links")
    if links is None:
        return violations
    if not isinstance(links, dict):
        violations.append(
            WorkflowViolation(
                rule_id="links_block_not_mapping",
                path="links",
                message=(f"`links:` must be a mapping; got {type(links).__name__}"),
                suggested_fix=(
                    "Emit `links:` as a mapping of "
                    "`<link_id>: { class: ..., config: { ... } }` entries."
                ),
            )
        )
        return violations

    for link_id, body in links.items():
        prefix = f"links.{link_id}"
        if not isinstance(body, dict):
            violations.append(
                WorkflowViolation(
                    rule_id="link_body_not_mapping",
                    path=prefix,
                    message=(f"link body must be a mapping; got {type(body).__name__}"),
                    suggested_fix=(
                        f"Use `{link_id}: {{ class: "
                        f'"nanobrain.core.link.DirectLink", config: '
                        "{ link_type: direct, source: ..., target: ..., "
                        "auto_transfer: true } }`."
                    ),
                )
            )
            continue

        class_path = body.get("class")
        link_config = body.get("config")

        if not class_path:
            violations.append(
                WorkflowViolation(
                    rule_id="link_class_missing",
                    path=f"{prefix}.class",
                    message="link has no `class:` field.",
                    suggested_fix=("Set `class:` to `nanobrain.core.link.DirectLink`."),
                )
            )
            continue

        class_str = str(class_path)
        if "TransformLink" in class_str:
            violations.append(
                WorkflowViolation(
                    rule_id="link_transformlink_forbidden",
                    path=f"{prefix}.class",
                    message=(
                        "`TransformLink` is forbidden — composer prompt "
                        "rule. LLMs hallucinate `transform_function` "
                        "import paths that don't exist."
                    ),
                    suggested_fix=(
                        "Use `nanobrain.core.link.DirectLink` and "
                        "author a novel-Python step to reshape data."
                    ),
                )
            )
            # Continue to catch auto_transfer / source / target issues too.

        target_class, import_err = _import_class(class_str)
        if target_class is None:
            violations.append(
                WorkflowViolation(
                    rule_id="link_class_unresolvable",
                    path=f"{prefix}.class",
                    message=(
                        f"link class path {class_path!r} could not be imported ({import_err})."
                    ),
                    suggested_fix=(
                        "Use `nanobrain.core.link.DirectLink` — that "
                        "is the only link class the composer prompt "
                        "permits."
                    ),
                )
            )
        elif not _is_link_subclass(target_class):
            violations.append(
                WorkflowViolation(
                    rule_id="link_class_not_link_subclass",
                    path=f"{prefix}.class",
                    message=(
                        f"{class_path!r} is not a subclass of `nanobrain.core.link.LinkBase`."
                    ),
                    suggested_fix=("Use `nanobrain.core.link.DirectLink`."),
                )
            )

        if not isinstance(link_config, dict):
            violations.append(
                WorkflowViolation(
                    rule_id="link_config_not_mapping",
                    path=f"{prefix}.config",
                    message=(
                        f"link `config:` must be an inline mapping "
                        f"(allowed because LinkBase is inline-eligible); "
                        f"got {type(link_config).__name__}"
                    ),
                    suggested_fix=(
                        "Use `config: { link_type: direct, source: "
                        '"...", target: "...", auto_transfer: true }`.'
                    ),
                )
            )
            continue

        # auto_transfer rule. The framework default is now True
        # (G7 Step 5, 2026-05-09), so omission is no longer a runtime
        # silent-failure. We still flag explicit `false` because that
        # re-opens the dominant silent-failure shape this validator
        # exists to prevent.
        auto = link_config.get("auto_transfer")
        if auto is False:
            violations.append(
                WorkflowViolation(
                    rule_id="link_auto_transfer_false",
                    path=f"{prefix}.config.auto_transfer",
                    message=(
                        "`auto_transfer: false` re-opens the dominant "
                        "silent-failure shape — the link will load "
                        "cleanly and every trigger fires, but no data "
                        "transfers and no exception is raised. The "
                        "workflow appears to run while producing no "
                        "output."
                    ),
                    suggested_fix=(
                        "Set `auto_transfer: true`. If you genuinely "
                        "want manual transfers, document why in a "
                        "comment and add a Python-side `link.transfer()` "
                        "call in a step's `process()`."
                    ),
                )
            )

        # Reference resolution — only checks well-formedness, not
        # data-unit-name existence on the target step (see comment in
        # _ref_target_is_step_qualified).
        source = link_config.get("source")
        target = link_config.get("target")
        for direction, ref in (("source", source), ("target", target)):
            if not isinstance(ref, str) or not ref:
                violations.append(
                    WorkflowViolation(
                        rule_id=f"link_{direction}_missing",
                        path=f"{prefix}.config.{direction}",
                        message=(f"`{direction}:` is missing or not a string."),
                        suggested_fix=(
                            f'Set `{direction}: "<step_id>.<du_name>"` '
                            "or a bare workflow-level data unit name "
                            "(e.g. `workflow_input` / `workflow_output`)."
                        ),
                    )
                )
                continue
            if _ref_target_is_workflow_level(ref, workflow_dict):
                continue
            if _ref_target_is_step_qualified(ref, workflow_dict):
                continue
            violations.append(
                WorkflowViolation(
                    rule_id=f"link_{direction}_dangling",
                    path=f"{prefix}.config.{direction}",
                    message=(
                        f"`{direction}: {ref!r}` points neither at a "
                        "workflow-level data unit nor at a step in "
                        "`steps:`."
                    ),
                    suggested_fix=(
                        "Bare names must match a key under "
                        "`input_data_units` or `output_data_units`. "
                        "Dotted names must start with a `steps:` key, "
                        "then `.<data_unit_name>`."
                    ),
                )
            )

    return violations


__all__ = [
    "WorkflowValidationError",
    "WorkflowViolation",
    "validate_workflow_against_framework",
]
