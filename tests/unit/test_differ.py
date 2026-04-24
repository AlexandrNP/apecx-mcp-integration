"""Unit tests for the T06 differ.

AP §5.6 AC1 — "a generated workflow with mixed steps produces a
categorization with one entry per step." Everything else (MCP tool
wording, scientist-time-to-decide) is tested at the composer /
integration layer or requires a human.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.differ import (
    StepCategory,
    categorize_workflow,
)

RETRIEVED = {
    "pkg.library.A",
    "pkg.library.B",
    "pkg.library.C",
}
YAML_PATHS = {
    "pkg.library.A": "steps/a.yml",
    "pkg.library.B": "steps/b.yml",
    "pkg.library.C": "steps/c.yml",
}


def test_step_with_canonical_config_path_is_composed_standard():
    wf = {
        "steps": {
            "s1": {"class": "pkg.library.A", "config": "steps/a.yml"},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert len(result.categorizations) == 1
    cat = result.categorizations[0]
    assert cat.category is StepCategory.COMPOSED_STANDARD
    assert cat.step_id == "s1"
    assert cat.step_class == "pkg.library.A"


def test_step_with_inline_config_mapping_is_parameterized():
    wf = {
        "steps": {
            "s1": {
                "class": "pkg.library.A",
                "config": {"top_k": 5, "model": "mistral"},
            },
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert result.categorizations[0].category is StepCategory.COMPOSED_PARAMETERIZED


def test_step_with_noncanonical_config_path_is_parameterized():
    wf = {
        "steps": {
            "s1": {"class": "pkg.library.A", "config": "steps/a_custom.yml"},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert result.categorizations[0].category is StepCategory.COMPOSED_PARAMETERIZED


def test_step_with_config_referencing_novel_is_wrapped():
    wf = {
        "steps": {
            "s1": {
                "class": "pkg.library.A",
                "config": {"preprocessor": "rogue"},
            },
            "rogue": {"class": "generated.Rogue", "config": {}},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"rogue": "class Rogue: ..."},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    cats = {c.step_id: c.category for c in result.categorizations}
    assert cats["s1"] is StepCategory.COMPOSED_WRAPPED
    assert cats["rogue"] is StepCategory.NOVEL


def test_step_whose_class_is_unknown_is_novel_orphan():
    wf = {
        "steps": {
            "s1": {"class": "made.up.Class", "config": "steps/x.yml"},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert result.categorizations[0].category is StepCategory.NOVEL
    assert "orphan" in result.categorizations[0].reason


def test_step_id_in_novel_python_always_overrides_class_match():
    """If the LLM put a step_id in novel_python AND gave it a library
    class path (a weird but possible state), novel_python wins — the
    actual executed code comes from the novel fence, not the class
    path."""
    wf = {
        "steps": {
            "s1": {"class": "pkg.library.A", "config": "steps/a.yml"},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"s1": "class S1: ..."},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert result.categorizations[0].category is StepCategory.NOVEL


def test_mixed_workflow_produces_one_row_per_step_ac1():
    """AP §5.6 AC1: one entry per step, mixed categorization."""
    wf = {
        "steps": {
            "std":   {"class": "pkg.library.A", "config": "steps/a.yml"},
            "param": {"class": "pkg.library.B", "config": {"k": 3}},
            "wrap":  {"class": "pkg.library.C", "config": {"p": "rogue"}},
            "nov":   {"class": "generated.X", "config": {}},
            "rogue": {"class": "generated.Rogue", "config": {}},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"nov": "class X: ...", "rogue": "class Rogue: ..."},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert len(result.categorizations) == 5  # one per step — AC1
    cats = {c.step_id: c.category for c in result.categorizations}
    assert cats["std"]   is StepCategory.COMPOSED_STANDARD
    assert cats["param"] is StepCategory.COMPOSED_PARAMETERIZED
    assert cats["wrap"]  is StepCategory.COMPOSED_WRAPPED
    assert cats["nov"]   is StepCategory.NOVEL
    assert cats["rogue"] is StepCategory.NOVEL


def test_summary_sentence_ap_5_6_format():
    wf = {
        "steps": {
            "a": {"class": "pkg.library.A", "config": "steps/a.yml"},
            "b": {"class": "pkg.library.B", "config": "steps/b.yml"},
            "c": {"class": "pkg.library.C", "config": "steps/c.yml"},
            "d": {"class": "pkg.library.A", "config": {"k": 1}},
            "e": {"class": "pkg.library.A", "config": {"p": "nov"}},
            "f": {"class": "generated.X", "config": {}},
            "nov": {"class": "generated.N", "config": {}},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"f": "class X: ...", "nov": "class N: ..."},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    s = result.summary_sentence
    assert "7 step(s)" in s
    assert "5 compose library components" in s
    assert "(3 standard + 1 parameterized + 1 wrapped)" in s
    assert "2 step(s) are novel Python requiring review" in s


def test_novel_step_ids_lists_only_novel():
    wf = {
        "steps": {
            "a": {"class": "pkg.library.A", "config": "steps/a.yml"},
            "b": {"class": "generated.X", "config": {}},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"b": "class X: ..."},
        retrieved_class_paths=RETRIEVED,
        catalog_yaml_paths=YAML_PATHS,
    )
    assert result.novel_step_ids() == ("b",)


def test_workflow_without_steps_block_raises_for_non_mapping():
    wf = {"steps": "not a mapping"}
    with pytest.raises(ValueError, match="steps"):
        categorize_workflow(
            workflow_dict=wf,
            novel_python={},
            retrieved_class_paths=RETRIEVED,
        )


def test_empty_workflow_returns_empty_categorization():
    result = categorize_workflow(
        workflow_dict={"steps": {}},
        novel_python={},
        retrieved_class_paths=RETRIEVED,
    )
    assert result.categorizations == ()
    assert "0 step(s)" in result.summary_sentence
