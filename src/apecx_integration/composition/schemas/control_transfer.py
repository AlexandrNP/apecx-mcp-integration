"""ControlTransfer — the structured "return of control to the frontier LLM" payload (RoC-1b).

When the deterministic internal side cannot proceed without a decision only the user-facing
(frontier) LLM should make — a missing/ill-typed parameter, an ambiguous entity, an unmet
prerequisite, or a decomposition choice — it returns a ``WorkflowResult`` with
``status="needs_input"`` carrying one of these. It is NOT an error and NOT a guess: it states
exactly what is needed and how to obtain it, so control crosses the boundary explicitly.

This GENERALIZES the shipped disambiguation HITL envelope (``reason="ambiguous_entity"`` is one
case). All models set ``extra='forbid'`` so a typo'd field fails loudly rather than shaping a
malformed transfer (workspace rule).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ControlReason = Literal[
    "missing_param",
    "ambiguous_entity",
    "needs_prerequisite",
    "decomposition_choice",
]


class ParamNeed(BaseModel):
    """One parameter the frontier LLM must supply (for ``missing_param``)."""

    model_config = ConfigDict(extra="forbid")

    param_name: str
    issue: Literal["missing", "ill_typed"] = "missing"
    param_schema: dict[str, Any] | None = None  # JSON-Schema fragment for this param
    obtain_via: str | None = (
        None  # how to get a value, e.g. "resolve virus→taxon_id via harmonized_search"
    )


class WorkflowNeed(BaseModel):
    """One workflow in a proposed plan (for ``decomposition_choice``)."""

    model_config = ConfigDict(extra="forbid")

    workflow: str
    required_inputs: list[str] = []
    provided: list[str] = []
    missing: list[str] = []


class NextAction(BaseModel):
    """The structured action the frontier LLM should take. The populated field depends on the
    reason; the rest stay None (kept as one model, extra='forbid', so the shape is auditable)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    params: list[ParamNeed] | None = None  # missing_param
    candidates: list[dict[str, Any]] | None = None  # ambiguous_entity (candidate taxa: iri/label)
    workflows: list[WorkflowNeed] | None = None  # decomposition_choice
    prerequisite: str | None = None  # needs_prerequisite


class ControlTransfer(BaseModel):
    """The full return-of-control payload attached to a ``needs_input`` WorkflowResult."""

    model_config = ConfigDict(extra="forbid")

    reason: ControlReason
    next_action: NextAction
    message: str


# --------------------------------------------------------------------------- #
# Builders — one per reason (so callers never hand-assemble the shape)
# --------------------------------------------------------------------------- #
def missing_param_transfer(
    params: list[ParamNeed], *, message: str | None = None
) -> ControlTransfer:
    names = [p.param_name for p in params]
    return ControlTransfer(
        reason="missing_param",
        next_action=NextAction(kind="provide_missing_params", params=params),
        message=message or f"Provide the missing/ill-typed parameter(s) {names}, then re-call.",
    )


def ambiguous_entity_transfer(
    candidates: list[dict[str, Any]], *, message: str | None = None
) -> ControlTransfer:
    return ControlTransfer(
        reason="ambiguous_entity",
        next_action=NextAction(kind="choose_candidate", candidates=candidates),
        message=message
        or "The term is ambiguous — choose one candidate (by canonical IRI) and re-call.",
    )


def needs_prerequisite_transfer(
    prerequisite: str, *, message: str | None = None
) -> ControlTransfer:
    return ControlTransfer(
        reason="needs_prerequisite",
        next_action=NextAction(kind="resolve_prerequisite", prerequisite=prerequisite),
        message=message or f"Resolve the prerequisite first: {prerequisite}.",
    )


def decomposition_choice_transfer(
    workflows: list[WorkflowNeed], *, message: str | None = None
) -> ControlTransfer:
    return ControlTransfer(
        reason="decomposition_choice",
        next_action=NextAction(kind="run_workflows", workflows=workflows),
        message=message
        or "Proposed plan — fill each workflow's missing inputs and run them via run_workflow.",
    )


__all__ = [
    "ControlReason",
    "ControlTransfer",
    "NextAction",
    "ParamNeed",
    "WorkflowNeed",
    "ambiguous_entity_transfer",
    "decomposition_choice_transfer",
    "missing_param_transfer",
    "needs_prerequisite_transfer",
]
