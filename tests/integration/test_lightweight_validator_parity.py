"""LW — WorkflowBuilder ↔ A1 validator parity.

Confirms that the three workflow-authoring paths described in
CLAUDE.md produce the same validation behavior:

  1. Hand-authored YAML + Workflow.from_config — framework validation
     at config-load time (already in place).
  2. LLM composer + compose() — A1 validator runs in the pipeline
     (covered by tests/unit/test_workflow_validator.py).
  3. Lightweight WorkflowBuilder + .load() — A1 validator now
     reachable via apecx_integration.composition.lightweight_validator
     (NEW in this task).

These tests pin the third path's parity using the real
nanobrain.lightweight.WorkflowBuilder API (no mocks).
"""

from __future__ import annotations

import pytest

try:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder  # noqa: F401

    _LW_AVAILABLE = True
except ImportError:
    _LW_AVAILABLE = False

from apecx_integration.composition.lightweight_validator import (
    validate_and_load,
    validate_lightweight_builder,
)
from apecx_integration.composition.workflow_validator import (
    WorkflowValidationError,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _LW_AVAILABLE,
        reason=(
            "nanobrain.lightweight.workflow_builder not importable — run under the project venv"
        ),
    ),
]


class _FakeBuilder:
    """Duck-typed substitute for WorkflowBuilder. Avoids depending
    on every detail of the real builder's API (which has many
    optional fields) while still exercising the validator path.
    """

    def __init__(self, config: dict) -> None:
        self._config = config

    def get_config(self) -> dict:
        return self._config

    def load(self):  # pragma: no cover — never reached in invalid path
        raise AssertionError("load() must not be called when validation rejected the workflow")


def test_lightweight_validator_accepts_valid_workflow():
    """A workflow with a real Step subclass + file-path config
    must pass validation without raising."""
    builder = _FakeBuilder(
        {
            "name": "lw_valid",
            "steps": {
                "rag_synth": {
                    "class": (
                        "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
                    ),
                    "config": "steps/rag_synthesis.yml",
                }
            },
            "links": {},
        }
    )
    assert validate_lightweight_builder(builder) == ()


def test_lightweight_validator_rejects_inline_dict_on_step():
    """The exact A1 rule must fire on lightweight-builder output —
    not just on LLM composer output."""
    builder = _FakeBuilder(
        {
            "name": "lw_invalid",
            "steps": {
                "entity_extraction": {
                    "class": (
                        "apecx_integration.composition.steps."
                        "db_integration_wrappers.EntityExtractionStep"
                    ),
                    "config": {"max_candidates": 10},
                }
            },
            "links": {},
        }
    )
    violations = validate_lightweight_builder(builder)
    rule_ids = [v.rule_id for v in violations]
    assert "step_inline_config_forbidden" in rule_ids


def test_validate_and_load_raises_before_calling_load():
    """The convenience wrapper must NOT call builder.load() when
    validation fails — otherwise the heavy from_config path runs
    after we already know the workflow is invalid."""
    builder = _FakeBuilder(
        {
            "name": "lw_invalid_for_validate_and_load",
            "steps": {
                "ghost": {
                    "class": "not.a.real.module.GhostStep",
                    "config": "steps/ghost.yml",
                }
            },
            "links": {},
        }
    )
    with pytest.raises(WorkflowValidationError) as excinfo:
        validate_and_load(builder)
    assert any(v.rule_id == "step_class_unresolvable" for v in excinfo.value.violations)


def test_validate_lightweight_builder_rejects_non_builder():
    """Duck-typed but not too duck-typed — at minimum the object
    must expose get_config(). A bare dict or string would silently
    pass validation if the helper didn't check."""

    class _NoConfig:
        pass

    with pytest.raises(TypeError):
        validate_lightweight_builder(_NoConfig())


def test_validate_lightweight_builder_rejects_non_dict_config():
    class _BadBuilder:
        def get_config(self):
            return "this should be a dict, not a string"

    with pytest.raises(TypeError):
        validate_lightweight_builder(_BadBuilder())
