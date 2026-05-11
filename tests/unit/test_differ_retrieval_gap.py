"""A2 — Differ disk-existence + import-resolves second pass.

These tests pin the retrieval-gap rescue behavior:

- A real on-disk Step subclass that missed retrieval is RESCUED into
  the composed_* tier (with ``retrieval_gap=True``) instead of being
  mis-labeled NOVEL/orphan.
- A class path that doesn't import stays NOVEL but with the precise
  "unresolvable class path" reason — distinguishing typo /
  hallucination from retrieval-recall.
- A class that imports but isn't a Step subclass stays NOVEL with
  the precise "exists but not Step subclass" reason.

The Automated_Workflow_Generation_Issues.md report cited
``SynthesisContextAssemblyStep`` as a concrete instance of the
retrieval-gap mis-label; the first test below pins that exact case.
"""

from __future__ import annotations

from apecx_integration.composition.differ import (
    StepCategory,
    categorize_workflow,
)


def test_real_step_missed_by_retrieval_is_rescued():
    """The exact failure shape from the issues doc: a real Step
    subclass on disk, missed by retrieval top-k, was mis-labeled
    NOVEL/orphan. After A2 it's COMPOSED_PARAMETERIZED with
    ``retrieval_gap=True``.
    """
    workflow_dict = {
        "steps": {
            "synth_assembly": {
                "class": (
                    "apecx_integration.composition.steps."
                    "synthesis_context_assembly_step."
                    "SynthesisContextAssemblyStep"
                ),
                "config": "steps/synthesis_context_assembly.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={},
        retrieved_class_paths=set(),  # empty: retrieval missed it
        catalog_yaml_paths={},
    )
    cat = result.categorizations[0]
    assert cat.category in (
        StepCategory.COMPOSED_STANDARD,
        StepCategory.COMPOSED_PARAMETERIZED,
        StepCategory.COMPOSED_WRAPPED,
    )
    assert cat.retrieval_gap is True
    # The reason MUST mention the recovery path so reviewers know
    # this is a retrieval-recall signal, not the happy path.
    assert "disk-import fallback" in cat.reason


def test_unresolvable_class_path_is_novel_with_typo_reason():
    workflow_dict = {
        "steps": {
            "ghost_step": {
                "class": "this.module.does.not.exist.GhostStep",
                "config": "steps/ghost.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={},
        retrieved_class_paths=set(),
        catalog_yaml_paths={},
    )
    cat = result.categorizations[0]
    assert cat.category is StepCategory.NOVEL
    assert cat.retrieval_gap is False
    assert "typo or hallucinated" in cat.reason


def test_class_exists_but_not_step_subclass_is_novel_with_precise_reason():
    """A class that imports but isn't a Step subclass — distinct from
    a typo. The reviewer needs to know it's a workflow bug, not a
    retrieval bug.
    """
    workflow_dict = {
        "steps": {
            "wrong_kind": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "config": "steps/du.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={},
        retrieved_class_paths=set(),
        catalog_yaml_paths={},
    )
    cat = result.categorizations[0]
    assert cat.category is StepCategory.NOVEL
    assert cat.retrieval_gap is False
    assert "not a subclass" in cat.reason
    assert "BaseStep" in cat.reason


def test_retrieval_gap_flag_surfaced_in_summary_sentence():
    """The summary_sentence is what the MCP UI / Slack notification
    shows. It MUST mention the retrieval gap so operators see the
    retrieval-recall signal without drilling into per-step details.
    """
    workflow_dict = {
        "steps": {
            "synth_assembly": {
                "class": (
                    "apecx_integration.composition.steps."
                    "synthesis_context_assembly_step."
                    "SynthesisContextAssemblyStep"
                ),
                "config": "steps/synthesis_context_assembly.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={},
        retrieved_class_paths=set(),
        catalog_yaml_paths={},
    )
    sentence = result.summary_sentence
    assert "disk-import fallback" in sentence
    assert "retrieval recall" in sentence


def test_retrieved_step_still_categorized_without_retrieval_gap_flag():
    """Sanity: the happy path (class IS in retrieval set) is not
    affected — retrieval_gap stays False. Otherwise every step would
    look like a retrieval problem.
    """
    workflow_dict = {
        "steps": {
            "rag_synth": {
                "class": (
                    "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
                ),
                "config": "steps/rag_synthesis.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={},
        retrieved_class_paths={
            "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
        },
        catalog_yaml_paths={
            "apecx_integration.composition.steps."
            "rag_synthesis_step.RagSynthesisStep": "steps/rag_synthesis.yml",
        },
    )
    cat = result.categorizations[0]
    assert cat.category is StepCategory.COMPOSED_STANDARD
    assert cat.retrieval_gap is False
    assert "disk-import fallback" not in cat.reason


def test_novel_python_id_short_circuits_retrieval_gap_check():
    """If the step_id is in the novel_python fence, the verdict is
    novel regardless of class-path import outcome — novel_python is
    the strongest evidence we have.
    """
    workflow_dict = {
        "steps": {
            "custom_step": {
                "class": (
                    "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
                ),
                "config": "steps/rag_synthesis.yml",
            }
        }
    }
    result = categorize_workflow(
        workflow_dict=workflow_dict,
        novel_python={"custom_step": "class Foo: ..."},
        retrieved_class_paths=set(),
        catalog_yaml_paths={},
    )
    cat = result.categorizations[0]
    assert cat.category is StepCategory.NOVEL
    assert cat.retrieval_gap is False
