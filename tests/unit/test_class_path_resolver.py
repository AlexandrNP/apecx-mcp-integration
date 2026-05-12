"""Unit tests for the catalog-grounded class-path resolver (CPR).

The resolver is the deterministic fix for the dominant LLM
hallucination shape (leaf class name correct, module path drifted).
Each rule_id in the verdict needs a test that fails loudly if the
behavior silently weakens.

Tests cover:
  - Exact match → resolves to itself.
  - Single leaf match → resolves to the catalog entry (the
    suffix-drop repair the issues doc / E2E observations need).
  - Two+ leaf matches → ambiguous, no resolution, candidates listed.
  - No leaf match → novel; near-leaf hints surfaced for "Did you
    mean?".
  - Empty input → novel.
  - Hint formatter mentions the candidates.
  - Workflow repair walks steps and rewrites only LEAF_MATCH cases.
"""

from __future__ import annotations

from apecx_integration.composition.class_path_resolver import (
    ClassPathRepair,
    ResolutionKind,
    hint_for_step_violation,
    repair_workflow_class_paths,
    resolve_class_path,
)


def test_exact_match_returns_exact():
    catalog = {"pkg.a.b.FooStep", "pkg.c.d.BarStep"}
    out = resolve_class_path("pkg.a.b.FooStep", catalog)
    assert out.kind is ResolutionKind.EXACT
    assert out.resolved == "pkg.a.b.FooStep"


def test_single_leaf_match_resolves_to_catalog_entry():
    """The issues-doc / E2E pattern: LLM dropped the ``_step`` suffix
    from the module path. Leaf class name is the same.
    """
    catalog = {
        "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep",
    }
    emitted = "apecx_integration.composition.steps.rag_synthesis.RagSynthesisStep"
    out = resolve_class_path(emitted, catalog)
    assert out.kind is ResolutionKind.LEAF_MATCH
    assert out.resolved == (
        "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
    )


def test_two_leaf_matches_is_ambiguous():
    """If two catalog entries share the same leaf class name (rare
    but possible), the resolver must NOT auto-pick — the risk of
    silent wrong substitution is higher than the cost of a retry.
    """
    catalog = {
        "pkg.a.FooStep",
        "pkg.b.FooStep",
    }
    out = resolve_class_path("pkg.invented.FooStep", catalog)
    assert out.kind is ResolutionKind.AMBIGUOUS
    assert out.resolved is None
    assert set(out.candidates) == catalog


def test_no_leaf_match_is_novel_with_near_hints():
    catalog = {
        "pkg.steps.rag_synthesis_step.RagSynthesisStep",
        "pkg.steps.entity_extraction_step.EntityExtractionStep",
    }
    # Hallucinated leaf — but shares a prefix with one of the
    # catalog leaves.
    out = resolve_class_path("pkg.fake.RagInventedStep", catalog)
    assert out.kind is ResolutionKind.NOVEL
    assert out.resolved is None
    # The "Rag" prefix should make RagSynthesisStep appear in
    # the near-leaf hints.
    assert any("RagSynthesisStep" in c for c in out.candidates)


def test_empty_emitted_is_novel():
    out = resolve_class_path("", {"pkg.FooStep"})
    assert out.kind is ResolutionKind.NOVEL


def test_repair_workflow_in_place_for_leaf_match():
    workflow = {
        "steps": {
            "rag": {
                "class": "pkg.steps.rag_synthesis.RagSynthesisStep",
                "config": "steps/rag_synthesis.yml",
            },
        },
    }
    catalog = {"pkg.steps.rag_synthesis_step.RagSynthesisStep"}
    repairs = repair_workflow_class_paths(workflow, catalog)
    assert workflow["steps"]["rag"]["class"] == ("pkg.steps.rag_synthesis_step.RagSynthesisStep")
    assert len(repairs) == 1
    r = repairs[0]
    assert isinstance(r, ClassPathRepair)
    assert r.step_id == "rag"
    assert r.kind is ResolutionKind.LEAF_MATCH


def test_repair_leaves_ambiguous_untouched():
    """When two catalog entries share the leaf, the validator must
    handle it via ``step_class_unresolvable``; repair must NOT pick
    one silently."""
    workflow = {
        "steps": {
            "x": {"class": "pkg.z.FooStep", "config": "x.yml"},
        }
    }
    catalog = {"pkg.a.FooStep", "pkg.b.FooStep"}
    repairs = repair_workflow_class_paths(workflow, catalog)
    assert repairs == []
    # workflow unchanged
    assert workflow["steps"]["x"]["class"] == "pkg.z.FooStep"


def test_repair_leaves_exact_match_untouched():
    workflow = {
        "steps": {
            "x": {"class": "pkg.a.FooStep", "config": "x.yml"},
        }
    }
    catalog = {"pkg.a.FooStep"}
    repairs = repair_workflow_class_paths(workflow, catalog)
    assert repairs == []
    assert workflow["steps"]["x"]["class"] == "pkg.a.FooStep"


def test_repair_ignores_missing_steps_block():
    workflow = {"name": "no_steps"}
    repairs = repair_workflow_class_paths(workflow, {"pkg.FooStep"})
    assert repairs == []


def test_repair_ignores_non_string_class():
    """Defense in depth — if the LLM put a list / dict / None as
    the class value, repair must not crash."""
    workflow = {
        "steps": {
            "x": {"class": None, "config": "x.yml"},
            "y": {"class": ["nested", "list"], "config": "y.yml"},
        }
    }
    repairs = repair_workflow_class_paths(workflow, {"pkg.FooStep"})
    assert repairs == []


def test_hint_for_ambiguous_lists_candidates():
    catalog = {"pkg.a.FooStep", "pkg.b.FooStep"}
    hint = hint_for_step_violation("pkg.z.FooStep", catalog)
    assert hint is not None
    assert "Did you mean" in hint
    assert "pkg.a.FooStep" in hint
    assert "pkg.b.FooStep" in hint


def test_hint_for_novel_with_near_leaf_returns_closest():
    catalog = {
        "pkg.steps.rag_synthesis_step.RagSynthesisStep",
        "pkg.steps.entity_extraction_step.EntityExtractionStep",
    }
    hint = hint_for_step_violation("pkg.fake.RagSomethingElse", catalog)
    assert hint is not None
    assert "RagSynthesisStep" in hint


def test_hint_returns_none_when_no_useful_candidates():
    catalog = {"pkg.a.AlphaStep"}
    hint = hint_for_step_violation("pkg.z.CompletelyDifferent", catalog)
    # No near-leaf match → no hint to offer.
    assert hint is None
