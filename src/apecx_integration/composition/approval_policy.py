"""T06 approval-policy gate.

Reads a YAML that maps each ``StepCategory`` to an action, then
evaluates a ``CategorizedWorkflow`` and tells the caller which
steps (if any) block on human review. The Control Plane's
``/approvals/*`` endpoints consume this to know whether to mark a
run auto-approved or stall it for a reviewer.

Kept intentionally small — AP §5.6 Risks explicitly warns this is
the UX most-likely to over-ship. The policy is a dict-level
indirection, not a DSL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from apecx_integration.composition.differ import (
    CategorizedWorkflow,
    StepCategorization,
    StepCategory,
)


class ApprovalAction(StrEnum):
    AUTO = "auto"
    REQUIRE_REVIEW = "require_review"
    REQUIRE_EXPERT_REVIEW = "require_expert_review"


@dataclass(frozen=True, kw_only=True)
class ApprovalDecision:
    """Result of ``ApprovalPolicy.evaluate``.

    ``auto_approved_steps`` + ``review_required_steps`` +
    ``expert_review_required_steps`` partition the workflow's steps.
    ``blocks`` is ``True`` iff ANY step requires a human (either
    plain or expert) — the single boolean the Control Plane needs
    to decide between auto-approve and stall.
    """

    auto_approved_steps: tuple[StepCategorization, ...]
    review_required_steps: tuple[StepCategorization, ...]
    expert_review_required_steps: tuple[StepCategorization, ...]
    # The semantic reviewer (actor-critic) REJECTED the composition. A workflow-level signal
    # (not per-step): even when every step is auto-approvable by category, a reviewer rejection
    # forces a human-review pause — the reviewer can now STOP a workflow, not just annotate it.
    # Default False = no reviewer ran / not applicable (backward-compatible with every existing
    # evaluate() caller that omits reviewer_approved).
    reviewer_rejected: bool = False

    @property
    def blocks(self) -> bool:
        return bool(
            self.review_required_steps
            or self.expert_review_required_steps
            or self.reviewer_rejected
        )

    @property
    def strongest_required_action(self) -> ApprovalAction:
        if self.expert_review_required_steps:
            return ApprovalAction.REQUIRE_EXPERT_REVIEW
        # A reviewer rejection is human-review-grade (not expert); expert still wins when a
        # category also demands it (handled above).
        if self.review_required_steps or self.reviewer_rejected:
            return ApprovalAction.REQUIRE_REVIEW
        return ApprovalAction.AUTO

    @property
    def pause_reason(self) -> str | None:
        """One-sentence rationale for why the workflow is paused.

        Returns ``None`` when the decision did not block — i.e., no
        review was required. When non-None, the sentence names the
        strongest required review action AND lists which step
        categories drove it, so the reviewer can route attention
        precisely. The bug this fixes is the
        Automated_Workflow_Generation_Issues.md report's framing
        confusion: status=PAUSED with novel_python_by_step={} reads
        as a contradiction because the only review-driver mentioned
        anywhere is "novel python." The pause-reason names the
        actual driver (parameterized config, wrapped, novel) so
        "PAUSED with no novel python" no longer looks like a bug.

        A2 (2026-05-11) retrieval-gap interaction: when a step is
        ``COMPOSED_PARAMETERIZED`` only because A2 rescued it via
        disk-import fallback, the reason includes that hint so the
        reviewer knows the pause might be a retrieval-quality
        false-positive rather than a real bespoke wrapper.
        """
        if not self.blocks:
            return None

        # Build a per-action breakdown — categories + step_ids for each.
        def _summarize(label: str, steps: tuple[StepCategorization, ...]) -> str:
            if not steps:
                return ""
            by_cat: dict[StepCategory, list[str]] = {}
            for s in steps:
                by_cat.setdefault(s.category, []).append(s.step_id)
            parts: list[str] = []
            for cat, ids in by_cat.items():
                # Mark retrieval-gap-rescued steps so reviewers can
                # spot them at a glance.
                ids_marked: list[str] = []
                ids_by_step = {s.step_id: s for s in steps}
                for sid in ids:
                    if ids_by_step[sid].retrieval_gap:
                        ids_marked.append(f"{sid}*")
                    else:
                        ids_marked.append(sid)
                parts.append(f"{cat.value}={','.join(sorted(ids_marked))}")
            return f"{label}: {'; '.join(sorted(parts))}"

        segments: list[str] = []
        if self.expert_review_required_steps:
            segments.append(
                _summarize(
                    "expert review",
                    self.expert_review_required_steps,
                )
            )
        if self.review_required_steps:
            segments.append(
                _summarize(
                    "review",
                    self.review_required_steps,
                )
            )
        if self.reviewer_rejected:
            segments.append("semantic reviewer REJECTED the composition")
        sentence = "Workflow paused: " + " | ".join(segments) + "."
        has_marked = any(
            s.retrieval_gap
            for s in (
                *self.review_required_steps,
                *self.expert_review_required_steps,
            )
        )
        if has_marked:
            sentence += (
                " Steps marked with `*` were classified via the A2 "
                "disk-import fallback (retrieval missed them) — the "
                "pause may be a retrieval-recall artifact rather than "
                "a real bespoke wrapper."
            )
        return sentence


class ApprovalPolicy:
    """Category → action map, loaded from YAML."""

    def __init__(self, *, mapping: Mapping[StepCategory, ApprovalAction]):
        # Defensive copy so callers can't mutate the policy after load.
        self._mapping: dict[StepCategory, ApprovalAction] = dict(mapping)
        missing = [c for c in StepCategory if c not in self._mapping]
        if missing:
            raise ValueError(
                "approval policy must map every StepCategory; missing: "
                + ", ".join(c.value for c in missing)
            )

    @classmethod
    def load(cls, path: Path) -> ApprovalPolicy:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(
                f"approval policy at {path} must be a YAML mapping; got {type(raw).__name__}"
            )
        mapping: dict[StepCategory, ApprovalAction] = {}
        allowed_cats = {c.value for c in StepCategory}
        allowed_acts = {a.value for a in ApprovalAction}
        for k, v in raw.items():
            if k not in allowed_cats:
                raise ValueError(
                    f"approval policy at {path}: unknown category "
                    f"{k!r} — allowed: {sorted(allowed_cats)}"
                )
            if v not in allowed_acts:
                raise ValueError(
                    f"approval policy at {path}: unknown action {v!r} "
                    f"for category {k!r} — allowed: {sorted(allowed_acts)}"
                )
            mapping[StepCategory(k)] = ApprovalAction(v)
        return cls(mapping=mapping)

    def action_for(self, category: StepCategory) -> ApprovalAction:
        return self._mapping[category]

    def evaluate(
        self,
        categorized: CategorizedWorkflow,
        *,
        reviewer_approved: bool | None = None,
    ) -> ApprovalDecision:
        """Map step categories → an approval decision.

        ``reviewer_approved`` threads the semantic reviewer's verdict into the gate: when it is
        ``False`` the decision blocks (PAUSED) even if every step is auto-approvable by category,
        so a reviewer REJECT actually stops the workflow. ``None`` (default) = no reviewer ran /
        not applicable, preserving the category-only behavior for every existing caller.
        """
        auto: list[StepCategorization] = []
        review: list[StepCategorization] = []
        expert: list[StepCategorization] = []
        for step in categorized.categorizations:
            action = self._mapping[step.category]
            if action is ApprovalAction.AUTO:
                auto.append(step)
            elif action is ApprovalAction.REQUIRE_REVIEW:
                review.append(step)
            else:
                expert.append(step)
        return ApprovalDecision(
            auto_approved_steps=tuple(auto),
            review_required_steps=tuple(review),
            expert_review_required_steps=tuple(expert),
            reviewer_rejected=reviewer_approved is False,
        )


__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalPolicy",
]
