"""Unit tests for the composer's framework-native pre-execution validator.

Each test pins ONE rule_id. The goal is that a future change which
silently weakens a rule fails a named test — the test names map
1:1 onto ``rule_id`` strings emitted by
``validate_workflow_against_framework``.

Tests deliberately use real framework imports (no mocks): the
validator's correctness IS its agreement with the framework, so
mocking ``_is_inline_config_supported`` would let the test pass while
the validator silently disagreed with reality.
"""

from __future__ import annotations

import yaml

from apecx_integration.composition.workflow_validator import (
    WorkflowValidationError,
    WorkflowViolation,
    validate_workflow_against_framework,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _violation_ids(
    violations: tuple[WorkflowViolation, ...],
) -> list[str]:
    return [v.rule_id for v in violations]


def _yaml(text: str) -> dict:
    return yaml.safe_load(text)


_GOOD_WORKFLOW = """
name: minimal_good_workflow
description: a syntactically + framework-legal workflow used as baseline
config_version: 2

input_data_units:
  workflow_input:
    class: nanobrain.core.data_unit.DataUnitMemory
    name: workflow_input
    persistent: false

output_data_units:
  workflow_output:
    class: nanobrain.core.data_unit.DataUnitMemory
    name: workflow_output
    persistent: false

steps:
  rag_synth:
    class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep
    config: steps/rag_synthesis.yml

links:
  in_to_step:
    class: nanobrain.core.link.DirectLink
    config:
      link_type: direct
      source: workflow_input
      target: rag_synth.synthesis_bundle_input
      auto_transfer: true

  step_to_out:
    class: nanobrain.core.link.DirectLink
    config:
      link_type: direct
      source: rag_synth.synthesis_markdown_output
      target: workflow_output
      auto_transfer: true
"""


# ---------------------------------------------------------------------------
# Baseline — happy path
# ---------------------------------------------------------------------------


def test_good_workflow_produces_no_violations():
    """A real, on-disk Step subclass + canonical link shape should
    pass cleanly. If this test fails, the validator is over-strict
    and will fail-loud on workflows that should pass.
    """
    violations = validate_workflow_against_framework(_yaml(_GOOD_WORKFLOW))
    assert violations == ()


# ---------------------------------------------------------------------------
# Issue #1 from Automated_Workflow_Generation_Issues.md — inline dict on Step
# ---------------------------------------------------------------------------


def test_step_inline_config_forbidden_on_real_step():
    """The exact failure shape from the issues doc: composer emitted
    ``config: { target_entities: [...], max_candidates: 10, ... }`` on
    a real Step class. The framework rejects this at runtime; the
    validator rejects it at compose-time.
    """
    workflow_dict = _yaml(
        """
        steps:
          entity_extraction:
            class: apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep
            config:
              target_entities: ['customer', 'product', 'feature']
              max_candidates: 10
              min_confidence: 0.5
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_inline_config_forbidden" in ids


def test_step_inline_config_allowed_on_data_unit():
    """A DataUnitMemory IS inline-eligible — placing it under steps:
    is incorrect for a different reason (it's not a Step subclass),
    but the inline-dict check itself must NOT fire.

    This test pins the framework rule that DataUnit/Link/Trigger are
    the carve-out. Without this pin, a future "tighten the validator"
    refactor that drops the carve-out would slip in silently.
    """
    workflow_dict = _yaml(
        """
        steps:
          data_holder:
            class: nanobrain.core.data_unit.DataUnitMemory
            config:
              name: data_holder
              persistent: false
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    # The misplacement (DataUnit under steps:) DOES surface as a
    # non-Step-subclass violation, but the inline-config check must
    # not fire.
    assert "step_inline_config_forbidden" not in ids
    assert "step_class_not_step_subclass" in ids


# ---------------------------------------------------------------------------
# Class resolution rules
# ---------------------------------------------------------------------------


def test_step_class_unresolvable_when_module_missing():
    workflow_dict = _yaml(
        """
        steps:
          ghost_step:
            class: this.module.does.not.exist.GhostStep
            config: steps/ghost.yml
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_class_unresolvable" in ids


def test_step_class_unresolvable_when_attribute_missing():
    workflow_dict = _yaml(
        """
        steps:
          missing_attr_step:
            class: apecx_integration.composition.steps.rag_synthesis_step.ThisClassDoesNotExist
            config: steps/foo.yml
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_class_unresolvable" in ids


def test_step_class_missing_raises_named_violation():
    workflow_dict = _yaml(
        """
        steps:
          forgot_class:
            config: steps/foo.yml
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_class_missing" in ids


# ---------------------------------------------------------------------------
# Catalog canonical-path check (soft violation)
# ---------------------------------------------------------------------------


def test_step_config_non_canonical_path_when_catalog_provided():
    workflow_dict = _yaml(
        """
        steps:
          rag_synth:
            class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep
            config: steps/some_typo_rag.yml
        """
    )
    catalog = {
        "apecx_integration.composition.steps.rag_synthesis_step."
        "RagSynthesisStep": "steps/rag_synthesis.yml",
    }
    violations = validate_workflow_against_framework(workflow_dict, catalog_yaml_paths=catalog)
    ids = _violation_ids(violations)
    assert "step_config_non_canonical_path" in ids


def test_canonical_path_check_skipped_when_no_catalog():
    workflow_dict = _yaml(
        """
        steps:
          rag_synth:
            class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep
            config: steps/some_typo_rag.yml
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_config_non_canonical_path" not in ids


# ---------------------------------------------------------------------------
# Link rules
# ---------------------------------------------------------------------------


def test_link_transformlink_forbidden():
    workflow_dict = _yaml(
        """
        steps: {}
        input_data_units:
          workflow_input:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_input
        output_data_units:
          workflow_output:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_output
        links:
          bad_transform:
            class: nanobrain.core.link.TransformLink
            config:
              link_type: transform
              source: workflow_input
              target: workflow_output
              transform_function: pkg.mod.fn
              auto_transfer: true
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "link_transformlink_forbidden" in ids


def test_link_auto_transfer_false_flagged():
    workflow_dict = _yaml(
        """
        steps: {}
        input_data_units:
          workflow_input:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_input
        output_data_units:
          workflow_output:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_output
        links:
          silent_link:
            class: nanobrain.core.link.DirectLink
            config:
              link_type: direct
              source: workflow_input
              target: workflow_output
              auto_transfer: false
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "link_auto_transfer_false" in ids


def test_link_auto_transfer_omitted_not_flagged_post_g7_step5():
    """G7 Step 5 (2026-05-09): the framework now defaults
    ``auto_transfer=True`` at field declaration. The validator MUST
    NOT raise on omission — that would force the LLM to emit the
    flag on every link even though the framework guarantees the
    safe default.

    If this test starts failing, either:
      a) The framework regressed the default back to False
         (re-introduces the silent-failure shape — investigate
         nanobrain core/link.py LinkConfig), or
      b) Someone tightened the validator. Don't — re-introduces
         the prompt-pedantry tradeoff.
    """
    workflow_dict = _yaml(
        """
        steps: {}
        input_data_units:
          workflow_input:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_input
        output_data_units:
          workflow_output:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_output
        links:
          implicit_link:
            class: nanobrain.core.link.DirectLink
            config:
              link_type: direct
              source: workflow_input
              target: workflow_output
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "link_auto_transfer_false" not in ids


def test_link_dangling_target():
    workflow_dict = _yaml(
        """
        steps:
          rag_synth:
            class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep
            config: steps/rag_synthesis.yml
        input_data_units:
          workflow_input:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_input
        output_data_units:
          workflow_output:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_output
        links:
          dangling:
            class: nanobrain.core.link.DirectLink
            config:
              link_type: direct
              source: workflow_input
              target: ghost_step.some_du
              auto_transfer: true
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "link_target_dangling" in ids


def test_link_dangling_source_bare_name():
    workflow_dict = _yaml(
        """
        steps: {}
        input_data_units:
          workflow_input:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_input
        output_data_units:
          workflow_output:
            class: nanobrain.core.data_unit.DataUnitMemory
            name: workflow_output
        links:
          bare_dangling:
            class: nanobrain.core.link.DirectLink
            config:
              link_type: direct
              source: not_a_real_workflow_du
              target: workflow_output
              auto_transfer: true
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "link_source_dangling" in ids


# ---------------------------------------------------------------------------
# Error class behavior
# ---------------------------------------------------------------------------


def test_validation_error_carries_violations_and_yaml():
    violations = (
        WorkflowViolation(
            rule_id="step_inline_config_forbidden",
            path="steps.x.config",
            message="msg",
            suggested_fix="fix",
        ),
    )
    err = WorkflowValidationError(violations, yaml_text="name: x\n")
    assert err.violations == violations
    assert err.yaml_text == "name: x\n"
    # Message must mention the rule_id (otherwise the LLM-facing
    # error has no signal for retry pattern-matching).
    assert "step_inline_config_forbidden" in str(err)


def test_validation_error_feedback_payload_is_llm_readable():
    """The payload format must be stable: the C1 retry loop pastes
    it back to the LLM verbatim. Reordering or losing the rule_id /
    path / fix triple breaks the retry's ability to point the LLM at
    a specific repair.
    """
    violations = (
        WorkflowViolation(
            rule_id="step_inline_config_forbidden",
            path="steps.entity_extraction.config",
            message="msg about inline dict",
            suggested_fix="use config: 'steps/entity_extraction.yml'",
        ),
        WorkflowViolation(
            rule_id="link_transformlink_forbidden",
            path="links.bad.class",
            message="TransformLink not allowed",
            suggested_fix="use DirectLink",
        ),
    )
    err = WorkflowValidationError(violations)
    payload = err.to_feedback_payload()
    assert "step_inline_config_forbidden" in payload
    assert "steps.entity_extraction.config" in payload
    assert "msg about inline dict" in payload
    assert "use config: 'steps/entity_extraction.yml'" in payload
    assert "link_transformlink_forbidden" in payload
    assert "DirectLink" in payload


# ---------------------------------------------------------------------------
# Structural edge cases
# ---------------------------------------------------------------------------


def test_top_level_not_mapping_short_circuits():
    violations = validate_workflow_against_framework(
        ["this", "is", "a", "list"]  # type: ignore[arg-type]
    )
    ids = _violation_ids(violations)
    assert ids == ["workflow_not_mapping"]


def test_empty_steps_dict_flagged_as_workflow_has_no_steps():
    """Real ollama E2E run on 2026-05-11 caught the LLM emitting a
    workflow with ``steps: {}``. Previous validator behavior: pass.
    New behavior: flag as a workflow_has_no_steps violation so the
    C1 retry loop gets a chance to repair it.
    """
    violations = validate_workflow_against_framework(
        {"name": "empty_workflow", "steps": {}, "links": {}}
    )
    assert "workflow_has_no_steps" in _violation_ids(violations)


def test_missing_steps_key_also_flagged_as_no_steps():
    """Same rule fires when ``steps:`` is omitted entirely — the
    framework's runtime check might surface this too, but A1 should
    catch it at compose-time so the C1 retry loop applies."""
    violations = validate_workflow_against_framework({"name": "no_steps_key_at_all"})
    assert "workflow_has_no_steps" in _violation_ids(violations)


def test_steps_block_not_mapping():
    workflow_dict = {"steps": ["a", "b"]}
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "steps_block_not_mapping" in ids


def test_links_block_not_mapping():
    workflow_dict = {"steps": {}, "links": "should be a dict"}
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "links_block_not_mapping" in ids


def test_step_body_not_mapping():
    workflow_dict = {"steps": {"my_step": "not a mapping"}}
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_body_not_mapping" in ids


# ---------------------------------------------------------------------------
# Multiple violations on the same workflow surface together
# ---------------------------------------------------------------------------


def test_multiple_violations_returned_together():
    """The validator must run to completion and surface every
    violation; halting on the first one would force a multi-turn
    repair cycle for what could be one-shot.
    """
    workflow_dict = _yaml(
        """
        steps:
          bad_inline:
            class: apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep
            config:
              max_candidates: 10
          ghost:
            class: not.a.real.module.GhostStep
            config: steps/ghost.yml
        links:
          bad_transform:
            class: nanobrain.core.link.TransformLink
            config:
              link_type: transform
              source: workflow_input
              target: bad_inline.entities_output
              auto_transfer: false
        """
    )
    violations = validate_workflow_against_framework(workflow_dict)
    ids = _violation_ids(violations)
    assert "step_inline_config_forbidden" in ids
    assert "step_class_unresolvable" in ids
    assert "link_transformlink_forbidden" in ids
    assert "link_auto_transfer_false" in ids
