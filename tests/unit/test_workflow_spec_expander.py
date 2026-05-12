"""Unit tests for ``apecx_integration.composition.workflow_spec``.

The expander is the LLM-side leverage point: a tiny spec
deterministically expands to a full framework-legal workflow. These
tests pin the deterministic contract so a future "simplify the
expander" refactor doesn't silently produce illegal output.

Tests cover:
  - Leaf class name resolves to catalog full path.
  - Full class path passes through unchanged.
  - Ambiguous leaf raises SpecExpansionError.
  - Unknown class raises SpecExpansionError.
  - Link class defaults to DirectLink, auto_transfer forced to True.
  - Workflow-level data units auto-scaffolded for bare-name endpoints.
  - config_override beats catalog canonical path.
  - link_type != "direct" surfaces a warning but is forced to direct.
  - novel_python steps emit a class path verbatim (no catalog lookup).
  - config_version: 2 is always emitted.

Pydantic ``extra='forbid'`` is exercised at the schema layer (a typo
in spec keys raises validation error).
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.component_catalog import CatalogComponent
from apecx_integration.composition.workflow_spec import (
    MinimalWorkflowSpec,
    SpecExpansionError,
    WorkflowLinkSpec,
    WorkflowStepSpec,
    expand_spec,
)


def _catalog() -> list[CatalogComponent]:
    return [
        CatalogComponent(
            id="rag_synthesis",
            name="RagSynthesisStep",
            description="LLM synthesis over assembled context",
            class_path=("apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"),
            yaml_path="steps/rag_synthesis.yml",
        ),
        CatalogComponent(
            id="synth_assembly",
            name="SynthesisContextAssemblyStep",
            description="Fan-in assembler for retrieval branches",
            class_path=(
                "apecx_integration.composition.steps."
                "synthesis_context_assembly_step."
                "SynthesisContextAssemblyStep"
            ),
            yaml_path="steps/synthesis_context_assembly.yml",
        ),
        CatalogComponent(
            id="entity_extraction",
            name="EntityExtractionStep",
            description="Entity NER",
            class_path=(
                "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
            ),
            yaml_path="steps/entity_extraction.yml",
        ),
    ]


def test_leaf_class_name_resolves_to_full_catalog_path():
    spec = MinimalWorkflowSpec(
        name="leaf_resolution",
        steps=[WorkflowStepSpec(id="rs", class_name="RagSynthesisStep")],
    )
    out, warnings = expand_spec(spec, _catalog())
    assert out["steps"]["rs"]["class"] == (
        "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
    )
    assert out["steps"]["rs"]["config"] == "steps/rag_synthesis.yml"
    # A warning records the resolution for the reviewer.
    assert any("resolved leaf class name" in w for w in warnings)


def test_full_dotted_class_path_passes_through():
    full = "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
    spec = MinimalWorkflowSpec(
        name="full_path",
        steps=[WorkflowStepSpec(id="rs", class_name=full)],
    )
    out, warnings = expand_spec(spec, _catalog())
    assert out["steps"]["rs"]["class"] == full
    assert all("resolved leaf class name" not in w for w in warnings)


def test_unknown_class_raises_expansion_error():
    spec = MinimalWorkflowSpec(
        name="bad",
        steps=[WorkflowStepSpec(id="ghost", class_name="CompletelyMadeUp")],
    )
    with pytest.raises(SpecExpansionError, match="no catalog match"):
        expand_spec(spec, _catalog())


def test_ambiguous_leaf_raises_expansion_error():
    """Two catalog entries share a leaf name → caller must use
    full dotted path. The expander refuses to guess."""
    catalog = [
        CatalogComponent(
            id="a",
            name="FooStep",
            description="",
            class_path="pkg.a.FooStep",
            yaml_path="steps/foo_a.yml",
        ),
        CatalogComponent(
            id="b",
            name="FooStep",
            description="",
            class_path="pkg.b.FooStep",
            yaml_path="steps/foo_b.yml",
        ),
    ]
    spec = MinimalWorkflowSpec(
        name="ambig",
        steps=[WorkflowStepSpec(id="x", class_name="FooStep")],
    )
    with pytest.raises(SpecExpansionError, match="ambiguous"):
        expand_spec(spec, catalog)


def test_link_class_defaults_to_directlink_with_auto_transfer():
    spec = MinimalWorkflowSpec(
        name="link_test",
        steps=[
            WorkflowStepSpec(id="a", class_name="RagSynthesisStep"),
            WorkflowStepSpec(id="b", class_name="SynthesisContextAssemblyStep"),
        ],
        links=[
            WorkflowLinkSpec(source="a.out", target="b.in"),
        ],
    )
    out, _ = expand_spec(spec, _catalog())
    links = out["links"]
    assert len(links) == 1
    link = next(iter(links.values()))
    assert link["class"] == "nanobrain.core.link.DirectLink"
    cfg = link["config"]
    assert cfg["link_type"] == "direct"
    assert cfg["source"] == "a.out"
    assert cfg["target"] == "b.in"
    # Auto-transfer is FORCED — the dominant silent-failure shape.
    assert cfg["auto_transfer"] is True


def test_bare_name_endpoints_scaffold_workflow_data_units():
    spec = MinimalWorkflowSpec(
        name="wf_dus",
        steps=[
            WorkflowStepSpec(id="step1", class_name="RagSynthesisStep"),
        ],
        links=[
            WorkflowLinkSpec(source="workflow_input", target="step1.synthesis_input"),
            WorkflowLinkSpec(source="step1.synthesis_markdown_output", target="workflow_output"),
        ],
    )
    out, _ = expand_spec(spec, _catalog())
    # Both workflow-level DUs auto-emit with sensible defaults.
    assert "workflow_input" in out["input_data_units"]
    assert "workflow_output" in out["output_data_units"]
    wi = out["input_data_units"]["workflow_input"]
    assert wi["class"] == "nanobrain.core.data_unit.DataUnitMemory"
    assert wi["persistent"] is False


def test_config_override_beats_canonical_path():
    spec = MinimalWorkflowSpec(
        name="override",
        steps=[
            WorkflowStepSpec(
                id="rs",
                class_name="RagSynthesisStep",
                config_override="steps/custom_rag.yml",
            )
        ],
    )
    out, _ = expand_spec(spec, _catalog())
    assert out["steps"]["rs"]["config"] == "steps/custom_rag.yml"


def test_non_direct_link_type_is_forced_with_warning():
    """The composer's prompt rules forbid TransformLink. If a spec
    asks for something other than 'direct', force to direct AND
    record a warning so the reviewer sees what happened."""
    spec = MinimalWorkflowSpec(
        name="bad_link",
        steps=[
            WorkflowStepSpec(id="a", class_name="RagSynthesisStep"),
        ],
        links=[
            WorkflowLinkSpec(source="x", target="y", link_type="transform"),
        ],
    )
    out, warnings = expand_spec(spec, _catalog())
    link = next(iter(out["links"].values()))
    assert link["config"]["link_type"] == "direct"
    assert any("forcing to direct" in w for w in warnings)


def test_novel_python_step_emits_class_path_verbatim():
    """Novel-Python steps don't need to be in the catalog — the
    LLM authors them as part of the spec. The expander emits the
    step entry with the LLM's class path AS-IS (no catalog lookup),
    and threads the source onto a private key the composer reads."""
    spec = MinimalWorkflowSpec(
        name="novel",
        steps=[
            WorkflowStepSpec(id="reshape", class_name="ReshapeStep"),
        ],
        novel_python={"reshape": "class ReshapeStep: ..."},
    )
    out, _ = expand_spec(spec, _catalog())
    assert out["steps"]["reshape"]["class"] == "ReshapeStep"
    assert out["_apecx_novel_python_by_step"] == {"reshape": "class ReshapeStep: ..."}


def test_config_version_2_always_emitted():
    spec = MinimalWorkflowSpec(name="cv", steps=[], links=[])
    out, _ = expand_spec(spec, _catalog())
    assert out["config_version"] == 2


def test_extra_top_level_key_rejected_by_schema():
    """Pydantic extra='forbid' must catch typos in the spec — that
    is the whole point of using a typed schema instead of a free
    dict for the LLM's output."""
    with pytest.raises(ValueError, match="extra_forbidden|stes"):
        MinimalWorkflowSpec.model_validate({"name": "x", "stes": [], "links": []})


def test_link_id_is_stable_and_includes_endpoints():
    spec = MinimalWorkflowSpec(
        name="x",
        steps=[],
        links=[
            WorkflowLinkSpec(source="a.b", target="c.d"),
            WorkflowLinkSpec(source="a.b", target="e.f"),
        ],
    )
    out, _ = expand_spec(spec, _catalog())
    link_ids = list(out["links"])
    assert "a_b_to_c_d_0" in link_ids
    assert "a_b_to_e_f_1" in link_ids
    assert len(set(link_ids)) == len(link_ids)
