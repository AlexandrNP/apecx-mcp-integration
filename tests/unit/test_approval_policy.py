"""T06 AC4 — approval-policy YAML is honored.

``composed_standard: auto`` actually auto-approves; ``novel:
require_expert_review`` actually blocks. Tests exercise the full
load → evaluate → decision pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.approval_policy import (
    ApprovalAction,
    ApprovalPolicy,
)
from apecx_integration.composition.differ import (
    CategorizedWorkflow,
    StepCategorization,
    StepCategory,
    categorize_workflow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


def _make_categorized(**counts) -> CategorizedWorkflow:
    """Build a CategorizedWorkflow with N steps per category."""
    cats = []
    for cat, n in counts.items():
        for i in range(n):
            cats.append(
                StepCategorization(
                    step_id=f"{cat.value}_{i}",
                    step_class="pkg.X",
                    category=cat,
                    reason="test",
                )
            )
    return CategorizedWorkflow(categorizations=tuple(cats))


def test_default_policy_loads_and_covers_every_category():
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    assert policy.action_for(StepCategory.COMPOSED_STANDARD) is ApprovalAction.AUTO
    assert policy.action_for(StepCategory.NOVEL) is ApprovalAction.REQUIRE_EXPERT_REVIEW


def test_evaluate_partitions_steps_by_action():
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    categorized = _make_categorized(
        **{
            StepCategory.COMPOSED_STANDARD: 2,
            StepCategory.COMPOSED_PARAMETERIZED: 1,
            StepCategory.NOVEL: 1,
        }
    )
    decision = policy.evaluate(categorized)
    assert len(decision.auto_approved_steps) == 2
    assert len(decision.review_required_steps) == 1
    assert len(decision.expert_review_required_steps) == 1
    assert decision.blocks is True


def test_all_auto_does_not_block_ac4_auto_branch():
    """AC4: ``composed_standard: auto`` actually auto-approves."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    categorized = _make_categorized(**{StepCategory.COMPOSED_STANDARD: 3})
    decision = policy.evaluate(categorized)
    assert decision.blocks is False
    assert decision.strongest_required_action is ApprovalAction.AUTO


def test_any_novel_blocks_ac4_review_branch():
    """AC4: ``novel: require_expert_review`` actually blocks."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    categorized = _make_categorized(
        **{
            StepCategory.COMPOSED_STANDARD: 5,
            StepCategory.NOVEL: 1,
        }
    )
    decision = policy.evaluate(categorized)
    assert decision.blocks is True
    assert decision.strongest_required_action is ApprovalAction.REQUIRE_EXPERT_REVIEW


def test_strongest_required_action_ladder():
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    # Only parameterized → plain review.
    c1 = _make_categorized(**{StepCategory.COMPOSED_PARAMETERIZED: 1})
    assert policy.evaluate(c1).strongest_required_action is ApprovalAction.REQUIRE_REVIEW
    # Mix of parameterized + novel → expert review wins.
    c2 = _make_categorized(
        **{
            StepCategory.COMPOSED_PARAMETERIZED: 2,
            StepCategory.NOVEL: 1,
        }
    )
    assert policy.evaluate(c2).strongest_required_action is ApprovalAction.REQUIRE_EXPERT_REVIEW


def test_policy_missing_category_rejected(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "composed_standard: auto\n"
        "composed_parameterized: require_review\n"
        "composed_wrapped: require_review\n",
        # novel: missing
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing"):
        ApprovalPolicy.load(bad)


def test_policy_unknown_category_rejected(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "composed_standard: auto\n"
        "composed_parameterized: require_review\n"
        "composed_wrapped: require_review\n"
        "novel: require_expert_review\n"
        "bogus_category: auto\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown category"):
        ApprovalPolicy.load(bad)


def test_policy_unknown_action_rejected(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "composed_standard: ignore_everything\n"
        "composed_parameterized: require_review\n"
        "composed_wrapped: require_review\n"
        "novel: require_expert_review\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown action"):
        ApprovalPolicy.load(bad)


def test_end_to_end_from_categorize_to_decision():
    """AC1 + AC4 integration smoke: categorize → evaluate → decision."""
    wf = {
        "steps": {
            "a": {"class": "pkg.library.A", "config": "steps/a.yml"},
            "b": {"class": "generated.X", "config": {}},
        }
    }
    cat = categorize_workflow(
        workflow_dict=wf,
        novel_python={"b": "class X: ..."},
        retrieved_class_paths={"pkg.library.A"},
        catalog_yaml_paths={"pkg.library.A": "steps/a.yml"},
    )
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    decision = policy.evaluate(cat)
    assert [s.step_id for s in decision.auto_approved_steps] == ["a"]
    assert [s.step_id for s in decision.expert_review_required_steps] == ["b"]
    assert decision.blocks is True


def test_reviewer_reject_forces_block_on_all_auto_workflow():
    """#6 (2026-07-01): a workflow whose steps are ALL auto-approvable by category still PAUSES
    when the semantic reviewer rejected it — the reviewer can now stop a workflow, not just
    annotate. Before this, the verdict was advisory and such a workflow auto-ran."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    categorized = _make_categorized(**{StepCategory.COMPOSED_STANDARD: 3})
    decision = policy.evaluate(categorized, reviewer_approved=False)
    assert decision.reviewer_rejected is True
    assert decision.blocks is True
    assert decision.strongest_required_action is ApprovalAction.REQUIRE_REVIEW


def test_reviewer_approve_none_preserves_category_only_behavior():
    """None (no reviewer ran) and True (approved) leave the category-only decision unchanged —
    backward compatibility for every existing caller that omits reviewer_approved."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    all_auto = _make_categorized(**{StepCategory.COMPOSED_STANDARD: 3})
    for verdict in (None, True):
        decision = policy.evaluate(all_auto, reviewer_approved=verdict)
        assert decision.reviewer_rejected is False
        assert decision.blocks is False
        assert decision.strongest_required_action is ApprovalAction.AUTO
    # A reviewer-approved workflow with a genuinely blocking category still blocks on the category.
    mixed = _make_categorized(**{StepCategory.COMPOSED_STANDARD: 2, StepCategory.NOVEL: 1})
    d = policy.evaluate(mixed, reviewer_approved=True)
    assert d.blocks is True
    assert d.strongest_required_action is ApprovalAction.REQUIRE_EXPERT_REVIEW
