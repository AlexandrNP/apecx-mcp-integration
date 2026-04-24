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
from enum import Enum
from pathlib import Path

import yaml

from apecx_integration.composition.differ import (
    CategorizedWorkflow,
    StepCategorization,
    StepCategory,
)


class ApprovalAction(str, Enum):
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

    @property
    def blocks(self) -> bool:
        return bool(
            self.review_required_steps
            or self.expert_review_required_steps
        )

    @property
    def strongest_required_action(self) -> ApprovalAction:
        if self.expert_review_required_steps:
            return ApprovalAction.REQUIRE_EXPERT_REVIEW
        if self.review_required_steps:
            return ApprovalAction.REQUIRE_REVIEW
        return ApprovalAction.AUTO


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
                f"approval policy at {path} must be a YAML mapping; "
                f"got {type(raw).__name__}"
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
        self, categorized: CategorizedWorkflow
    ) -> ApprovalDecision:
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
        )


__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalPolicy",
]
