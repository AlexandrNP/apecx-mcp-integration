"""DesignGateStep — terminal approval gate for the evidence-review workflow.

This is the FAN-IN terminal step. It receives two inputs via an
``AllDataReceivedTrigger``:

- ``review_in``  — the synthesized evidence ``{"markdown": ...}`` from
  ``EvidenceReviewSynthesisStep``;
- ``control_in`` — the ORIGINAL workflow input ``{query, requested_outputs,
  design_approval_id, ...}``, routed straight from ``workflow_input`` (the reused
  assembly/synthesis steps drop these control fields, so they reach the gate via a
  separate fan-in edge — the nanobrain-native answer to threading control state).

It emits the terminal ``WorkflowResult`` directly (it IS the envelope for this
workflow), choosing the disposition:

- ``requested_outputs != "evidence_plus_design"`` → status ``ok``, evidence only.
- design requested but no/invalid ``design_approval_id`` → status ``needs_input``
  with a ``needs_prerequisite`` control transfer, AND a fresh server-issued token to
  approve. Approval is EXPLICIT DATA — never inferred from the query text. The
  evidence is still returned in the markdown (degrade-loud: a pause never discards
  the evidence already gathered).
- design requested WITH a VALIDATED approval token → status ``ok`` with an appended,
  clearly-labelled design-hypotheses section (Phase B: LLM-generated, evidence-
  bound; until the generator lands this is a labelled placeholder).

Approval-token contract (2026-06-14, FAIL-CLOSED): ``design_approval_id`` is validated
against the ``DesignApprovalStore`` — the gate opens ONLY for a token that was
server-ISSUED (by this gate's needs_input path), operator-APPROVED (via the
``approve_design`` MCP tool), AND scope-bound to THIS request's ``(query, protein)``.
Any unknown / pending / rejected / scope-mismatched token withholds design with a named
reason. This closes the prior bypass where any non-blank string opened the gate. A
DEDICATED store (not the control-plane ``Approval`` model) because that model is
run/step-centric and this workflow runs via the direct MCP ``run_workflow`` path with no
control-plane run/step context (closed-class rule: new class when the existing one does
not fit). See ``composition/runtime/design_approval_store.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.schemas.control_transfer import needs_prerequisite_transfer

log = logging.getLogger(__name__)

_REVIEW_KEY = "review_in"
_CONTROL_KEY = "control_in"
_DESIGN = "evidence_plus_design"


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


class DesignGateStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class DesignGateStep(BaseStep):
    COMPONENT_TYPE: str = "design_gate_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return DesignGateStepConfig

    def _design_section(self, query: str, evidence_md: str, approval_id: str) -> str:
        """Generate the approved design-hypotheses section. Phase A: a labelled
        placeholder that carries approval provenance. Phase B replaces the body with
        an evidence-bound LLM generation (config-driven prompt).

        Honesty contract: a token reaching here has been VALIDATED by the gate's
        fail-closed check — it is server-issued, operator-approved, AND scope-bound to
        this query/protein (see ``DesignApprovalStore.validate``). So "(approved)" is
        accurate here; the section states the verification explicitly so the assurance
        is neither over- nor under-claimed."""
        return (
            "## Design / optimization hypotheses (approved)\n\n"
            f"> Released under design-approval token `{approval_id}` — verified "
            "server-side (operator-approved + scope-bound to this query/protein). These "
            "are **hypotheses**, not validated results — each must be confirmed "
            "experimentally.\n\n"
            "_Evidence-bound hypothesis generation is being wired in Phase B; this "
            "section confirms the approval gate opened (validated) and provenance is "
            "attached._"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"DesignGateStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Fan-in shape: {review_in: {...}, control_in: {...}}. Fail loud if either
        # leg is missing — a silent half-fan-in would drop evidence or control.
        review = input_data.get(_REVIEW_KEY)
        control = input_data.get(_CONTROL_KEY)
        if not isinstance(review, dict) or not isinstance(control, dict):
            raise ValueError(
                f"DesignGateStep '{self.name}': expected fan-in inputs "
                f"{_REVIEW_KEY!r} and {_CONTROL_KEY!r} (both dicts); got keys "
                f"{sorted(input_data)} (review={type(review).__name__}, "
                f"control={type(control).__name__})"
            )

        evidence_md = review.get("markdown")
        if not isinstance(evidence_md, str) or not evidence_md.strip():
            raise ValueError(
                f"DesignGateStep '{self.name}': review_in must carry a non-empty "
                f"'markdown' string; got {type(evidence_md).__name__}"
            )
        # E3-8: the per-run provenance record rides along from the review step; pass it
        # through unchanged to the terminal EnvelopeStep (the gate does not author it).
        provenance = review.get("provenance")
        # Forward the review's STRUCTURED output (a DataShape bundle) to the EnvelopeStep so it
        # surfaces as WorkflowResult.data_preview — emitted alongside the user-facing markdown.
        structured = review.get("data")

        requested = control.get("requested_outputs") or "evidence_only"
        approval_id = control.get("design_approval_id")
        query = control.get("query") or ""
        protein = control.get("protein")

        # Emit {markdown, control_transfer?, provenance?} for the terminal EnvelopeStep,
        # which shapes the WorkflowResult (and is the form run_workflow recognizes). A
        # control_transfer present → EnvelopeStep returns status=needs_input.
        if requested != _DESIGN:
            log.info("DesignGateStep %s: evidence_only → ok", self.name)
            return {"markdown": evidence_md, "data": structured, "provenance": provenance}

        # FAIL-CLOSED HITL validation (2026-06-14): the design path opens ONLY for a
        # server-issued, operator-approved token whose scope matches THIS request. A blank /
        # unknown / pending / rejected / scope-mismatched token withholds design with a NAMED
        # reason. Closes the prior bypass where ANY non-blank string opened the gate.
        store = get_design_approval_store()
        ok, reason = store.validate(token=approval_id, query=query, protein=protein)
        if ok:
            design_md = self._design_section(query, evidence_md, str(approval_id))
            log.info("DesignGateStep %s: design approval validated → ok with design", self.name)
            return {
                "markdown": f"{evidence_md.rstrip()}\n\n{design_md}\n",
                "data": structured,
                "provenance": provenance,
            }

        # Withheld. Issue a FRESH token to approve when none/unknown was supplied; otherwise
        # point at the supplied token + the specific reason it did not open.
        if _is_blank(approval_id) or "unknown" in reason:
            token = store.request(query=query, protein=protein)
            how = (
                f"A design-approval request has been issued: token `{token}`. Approve it via "
                f"the `approve_design` MCP tool, then re-call this workflow with "
                f"design_approval_id=`{token}` (and the SAME query + protein)."
            )
        else:
            token = str(approval_id)
            how = (
                f"Token `{token}` is not usable: {reason}. Get it approved via `approve_design` "
                "(or request a fresh one by re-calling without design_approval_id), then re-call."
            )
        md = (
            f"{evidence_md.rstrip()}\n\n"
            "## Design / optimization output — WITHHELD\n\n"
            f"> Design/optimization output was requested but the approval gate did not open: "
            f"{reason}. Approval must be EXPLICIT and is verified server-side — a token is not "
            f"valid merely by being present. {how} The evidence above is complete and unaffected."
        )
        ct = needs_prerequisite_transfer(
            "design_approval",
            message=(
                f"Design/optimization output requires an APPROVED, scope-bound "
                f"design_approval_id ({reason}). {how}"
            ),
        )
        log.info("DesignGateStep %s: design withheld — %s", self.name, reason)
        return {
            "markdown": md,
            "data": structured,
            "control_transfer": ct.model_dump(mode="json"),
            "provenance": provenance,
        }
