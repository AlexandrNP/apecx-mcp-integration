"""A3 — ApprovalDecision.pause_reason.

The issues doc framed ``status: PAUSED`` with ``novel_python_by_step:
{}`` as a contradiction. The pause is legitimate (parameterized /
wrapped composed steps required review), but the response carried
no signal naming that — only the novel-python field, which was empty.

These tests pin the precise wording so a future refactor that quietly
drops the category names or the step_ids regresses to the original
issues-doc framing confusion.
"""

from __future__ import annotations

from apecx_integration.composition.approval_policy import (
    ApprovalDecision,
)
from apecx_integration.composition.differ import (
    StepCategorization,
    StepCategory,
)


def _step(
    step_id: str,
    category: StepCategory,
    *,
    retrieval_gap: bool = False,
) -> StepCategorization:
    return StepCategorization(
        step_id=step_id,
        step_class=f"pkg.lib.{step_id.title()}",
        category=category,
        reason="fixture",
        retrieval_gap=retrieval_gap,
    )


def test_pause_reason_none_when_auto_approved():
    decision = ApprovalDecision(
        auto_approved_steps=(_step("s1", StepCategory.COMPOSED_STANDARD),),
        review_required_steps=(),
        expert_review_required_steps=(),
    )
    assert decision.pause_reason is None


def test_pause_reason_names_review_category_and_step_ids():
    """The fix for the issues-doc framing: the sentence must
    explicitly state WHICH steps required WHICH review action.
    """
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(
            _step("entity_extraction", StepCategory.COMPOSED_PARAMETERIZED),
            _step("synth_assembly", StepCategory.COMPOSED_PARAMETERIZED),
        ),
        expert_review_required_steps=(),
    )
    reason = decision.pause_reason
    assert reason is not None
    assert "Workflow paused" in reason
    assert "review" in reason
    assert "composed_parameterized" in reason
    assert "entity_extraction" in reason
    assert "synth_assembly" in reason


def test_pause_reason_mentions_expert_review_separately():
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(_step("param_step", StepCategory.COMPOSED_PARAMETERIZED),),
        expert_review_required_steps=(_step("novel_step", StepCategory.NOVEL),),
    )
    reason = decision.pause_reason
    assert reason is not None
    assert "expert review" in reason
    assert "novel_step" in reason
    assert "param_step" in reason


def test_pause_reason_marks_retrieval_gap_steps():
    """When the pause driver is a step rescued by A2's disk-import
    fallback, the reason must flag it so reviewers know the pause
    might be a retrieval-recall artifact.
    """
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(
            _step(
                "rescued_step",
                StepCategory.COMPOSED_PARAMETERIZED,
                retrieval_gap=True,
            ),
        ),
        expert_review_required_steps=(),
    )
    reason = decision.pause_reason
    assert reason is not None
    assert "rescued_step*" in reason
    assert "disk-import fallback" in reason
    assert "retrieval-recall artifact" in reason


def test_pause_reason_does_not_mention_disk_fallback_when_no_gap():
    """Sanity: the retrieval-recall hint must NOT appear on
    pause-reasons where no step carries the gap flag. Otherwise
    every pause looks like a retrieval problem.
    """
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(_step("ordinary", StepCategory.COMPOSED_PARAMETERIZED),),
        expert_review_required_steps=(),
    )
    reason = decision.pause_reason
    assert reason is not None
    assert "disk-import fallback" not in reason
    assert "retrieval-recall artifact" not in reason


def test_pause_reason_is_stable_across_reorderings():
    """The sentence assembles step_ids via sorted() so a future
    change in dict-iteration order doesn't flap the wording.
    """
    s1 = _step("zeta", StepCategory.COMPOSED_PARAMETERIZED)
    s2 = _step("alpha", StepCategory.COMPOSED_PARAMETERIZED)
    s3 = _step("mu", StepCategory.COMPOSED_PARAMETERIZED)

    d1 = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(s1, s2, s3),
        expert_review_required_steps=(),
    )
    d2 = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(s3, s1, s2),
        expert_review_required_steps=(),
    )
    assert d1.pause_reason == d2.pause_reason


def test_pause_reason_names_reviewer_rejection():
    """#6: when only the semantic reviewer rejected (no category driver), pause_reason still
    blocks and names the reviewer so a PAUSED run with empty novel_python is not a mystery."""
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(),
        reviewer_rejected=True,
    )
    assert decision.blocks is True
    reason = decision.pause_reason
    assert reason is not None
    assert "reviewer" in reason.lower()
    assert "REJECTED" in reason


def test_pause_reason_joins_reviewer_and_category_drivers():
    """When BOTH a category and the reviewer drive the pause, both segments are named."""
    decision = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(_step("novel_0", StepCategory.NOVEL),),
        reviewer_rejected=True,
    )
    reason = decision.pause_reason
    assert reason is not None
    assert "reviewer" in reason.lower()
    assert "expert review" in reason  # the category-driven segment is still present
