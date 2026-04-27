"""Probe batch 47 — adversarial probes against differ.categorize_workflow
+ CategorizedWorkflow + StepCategorization.

Streak before this batch: 174/300 post-AQ post-1066.
Probe naming: 1230–1254.

Distinct probes only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.differ import (
    CategorizedWorkflow,
    StepCategorization,
    StepCategory,
    categorize_workflow,
)


pytestmark = pytest.mark.integration


def _cat(step_id: str, category: StepCategory) -> StepCategorization:
    return StepCategorization(
        step_id=step_id, step_class="X.Y", category=category,
        reason="probe fixture",
    )


# --------------------------------------------------------------------------- #
# Probes 1230–1254
# --------------------------------------------------------------------------- #


def test_probe_1230_step_categorization_is_frozen():
    s = _cat("a", StepCategory.NOVEL)
    with pytest.raises(Exception):
        s.step_id = "b"  # type: ignore[misc]


def test_probe_1231_categorized_workflow_is_frozen():
    cw = CategorizedWorkflow(categorizations=(_cat("a", StepCategory.NOVEL),))
    with pytest.raises(Exception):
        cw.categorizations = ()  # type: ignore[misc]


def test_probe_1232_categorize_workflow_rejects_non_dict_steps():
    with pytest.raises(ValueError, match="must be a mapping"):
        categorize_workflow(
            workflow_dict={"steps": "not a mapping"},
            novel_python={},
            retrieved_class_paths=set(),
        )


def test_probe_1233_categorize_workflow_handles_empty_steps():
    cw = categorize_workflow(
        workflow_dict={"steps": {}},
        novel_python={},
        retrieved_class_paths=set(),
    )
    assert cw.categorizations == ()


def test_probe_1234_categorize_step_with_novel_python_id_is_novel():
    """Any step_id in novel_python is novel regardless of class path."""
    cw = categorize_workflow(
        workflow_dict={"steps": {"S1": {"class": "library.X"}}},
        novel_python={"S1": "def foo(): pass"},
        retrieved_class_paths={"library.X"},
    )
    cats = cw.categorizations
    assert len(cats) == 1
    assert cats[0].category is StepCategory.NOVEL


def test_probe_1235_categorize_step_class_in_retrieved_set_is_composed():
    """A step whose class is in retrieved_class_paths is composed_*
    (not novel)."""
    cw = categorize_workflow(
        workflow_dict={"steps": {"S1": {"class": "library.X"}}},
        novel_python={},
        retrieved_class_paths={"library.X"},
    )
    assert cw.categorizations[0].category in {
        StepCategory.COMPOSED_STANDARD,
        StepCategory.COMPOSED_PARAMETERIZED,
        StepCategory.COMPOSED_WRAPPED,
    }


def test_probe_1236_categorize_step_class_not_in_retrieved_is_novel():
    cw = categorize_workflow(
        workflow_dict={"steps": {"S1": {"class": "unknown.X"}}},
        novel_python={},
        retrieved_class_paths=set(),
    )
    assert cw.categorizations[0].category is StepCategory.NOVEL


def test_probe_1237_summary_sentence_includes_step_count():
    cats = (
        _cat("a", StepCategory.COMPOSED_STANDARD),
        _cat("b", StepCategory.NOVEL),
    )
    cw = CategorizedWorkflow(categorizations=cats)
    s = cw.summary_sentence
    assert "2 step" in s


def test_probe_1238_summary_sentence_includes_per_subcategory_counts():
    """The summary must break composed into standard / parameterized /
    wrapped — not just lump them. Pin the format."""
    cats = (
        _cat("a", StepCategory.COMPOSED_STANDARD),
        _cat("b", StepCategory.COMPOSED_PARAMETERIZED),
        _cat("c", StepCategory.COMPOSED_WRAPPED),
    )
    cw = CategorizedWorkflow(categorizations=cats)
    s = cw.summary_sentence
    assert "1 standard" in s
    assert "1 parameterized" in s
    assert "1 wrapped" in s


def test_probe_1239_count_returns_zero_for_absent_category():
    cw = CategorizedWorkflow(categorizations=(
        _cat("a", StepCategory.NOVEL),
    ))
    assert cw.count(StepCategory.COMPOSED_STANDARD) == 0


def test_probe_1240_count_returns_correct_for_repeated_category():
    cw = CategorizedWorkflow(categorizations=(
        _cat("a", StepCategory.NOVEL),
        _cat("b", StepCategory.NOVEL),
        _cat("c", StepCategory.NOVEL),
    ))
    assert cw.count(StepCategory.NOVEL) == 3


def test_probe_1241_novel_step_ids_returns_only_novel():
    cw = CategorizedWorkflow(categorizations=(
        _cat("a", StepCategory.COMPOSED_STANDARD),
        _cat("b", StepCategory.NOVEL),
        _cat("c", StepCategory.NOVEL),
    ))
    assert cw.novel_step_ids() == ("b", "c")


def test_probe_1242_novel_step_ids_returns_tuple_not_list():
    """novel_step_ids must return a tuple (immutable)."""
    cw = CategorizedWorkflow(categorizations=(_cat("a", StepCategory.NOVEL),))
    assert isinstance(cw.novel_step_ids(), tuple)


def test_probe_1243_summary_sentence_handles_empty_workflow():
    cw = CategorizedWorkflow(categorizations=())
    s = cw.summary_sentence
    assert "0 step" in s


def test_probe_1244_categorize_step_without_class_attribute():
    """A step dict without 'class' key — heuristic must handle it.
    Such a step has no class to look up; categorize as novel
    (defensive) or raise. Pin current behavior."""
    cw = categorize_workflow(
        workflow_dict={"steps": {"S1": {"config": "x.yml"}}},  # no 'class'
        novel_python={},
        retrieved_class_paths=set(),
    )
    # No class -> not in retrieved set -> novel.
    assert cw.categorizations[0].category is StepCategory.NOVEL


def test_probe_1245_categorize_with_catalog_paths_distinguishes_standard_vs_parameterized():
    """When catalog_yaml_paths is provided, a step whose config==canonical
    path is COMPOSED_STANDARD; deviation is COMPOSED_PARAMETERIZED."""
    catalog = {"library.X": "steps/canonical.yml"}
    cw = categorize_workflow(
        workflow_dict={"steps": {
            "S1": {"class": "library.X", "config": "steps/canonical.yml"},
            "S2": {"class": "library.X", "config": "steps/custom.yml"},
        }},
        novel_python={},
        retrieved_class_paths={"library.X"},
        catalog_yaml_paths=catalog,
    )
    cats_by_id = {c.step_id: c.category for c in cw.categorizations}
    assert cats_by_id["S1"] == StepCategory.COMPOSED_STANDARD
    assert cats_by_id["S2"] == StepCategory.COMPOSED_PARAMETERIZED


def test_probe_1246_categorize_without_catalog_paths_collapses_subcategories():
    """When catalog_yaml_paths is None, the heuristic can only tell
    composed_* (generic) from novel — pin the graceful degradation."""
    cw = categorize_workflow(
        workflow_dict={"steps": {
            "S1": {"class": "library.X", "config": "anything.yml"},
        }},
        novel_python={},
        retrieved_class_paths={"library.X"},
        catalog_yaml_paths=None,
    )
    # Without catalog paths, the category is one of the composed_*
    # variants (but not NOVEL).
    assert cw.categorizations[0].category in {
        StepCategory.COMPOSED_STANDARD,
        StepCategory.COMPOSED_PARAMETERIZED,
        StepCategory.COMPOSED_WRAPPED,
    }


def test_probe_1247_categorize_preserves_step_order():
    """The output's categorizations tuple must preserve the input
    step order (LLM-prompted summaries depend on consistent
    ordering)."""
    cw = categorize_workflow(
        workflow_dict={"steps": {
            "Z": {"class": "X.A"},
            "A": {"class": "X.A"},
            "M": {"class": "X.A"},
        }},
        novel_python={},
        retrieved_class_paths=set(),
    )
    ids = [c.step_id for c in cw.categorizations]
    # Python dicts preserve insertion order; must be ['Z','A','M'].
    assert ids == ["Z", "A", "M"]


def test_probe_1248_categorize_one_categorization_per_step():
    """No duplicates / no missing rows. ``len(categorizations) ==
    len(steps)``."""
    steps = {f"S{i}": {"class": "X.A"} for i in range(7)}
    cw = categorize_workflow(
        workflow_dict={"steps": steps},
        novel_python={},
        retrieved_class_paths=set(),
    )
    assert len(cw.categorizations) == 7


def test_probe_1249_categorize_step_with_int_class_name_handled():
    """A step dict with non-string class (int) — defensive handling."""
    # Either rejected at categorize_workflow or treated as 'not in
    # retrieved set'. Pin no-crash behavior.
    try:
        cw = categorize_workflow(
            workflow_dict={"steps": {"S1": {"class": 42}}},
            novel_python={},
            retrieved_class_paths=set(),
        )
        # Acceptable: novel category (42 not in retrieved set).
        assert cw.categorizations[0].category is StepCategory.NOVEL
    except (TypeError, ValueError):
        # Acceptable: typed rejection.
        pass


def test_probe_1250_step_categorization_reason_is_str():
    s = _cat("a", StepCategory.NOVEL)
    assert isinstance(s.reason, str)


def test_probe_1251_step_category_enum_string_inheritance():
    """StepCategory members carry string values for serialization."""
    for c in StepCategory:
        assert isinstance(c.value, str)


def test_probe_1252_categorize_workflow_returns_CategorizedWorkflow_type():
    cw = categorize_workflow(
        workflow_dict={"steps": {}},
        novel_python={},
        retrieved_class_paths=set(),
    )
    assert isinstance(cw, CategorizedWorkflow)


def test_probe_1253_categorized_workflow_categorizations_is_tuple():
    """The .categorizations field must be a tuple (frozen +
    hashable) — a list would let callers mutate."""
    cw = CategorizedWorkflow(categorizations=(_cat("a", StepCategory.NOVEL),))
    assert isinstance(cw.categorizations, tuple)


def test_probe_1254_categorize_workflow_steps_key_is_optional():
    """A workflow_dict without 'steps' key — defensive handling.
    The function uses ``.get("steps") or {}``, so missing 'steps'
    is treated as empty (not error)."""
    cw = categorize_workflow(
        workflow_dict={"name": "wf-without-steps-key"},
        novel_python={},
        retrieved_class_paths=set(),
    )
    assert cw.categorizations == ()
