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
- design requested but no/blank ``design_approval_id`` → status ``needs_input``
  with a ``needs_prerequisite`` control transfer. Approval is EXPLICIT DATA — it is
  never inferred from the query text. The evidence is still returned in the
  markdown (degrade-loud: a pause never discards the evidence already gathered).
- design requested WITH an approval token → status ``ok`` with an appended,
  clearly-labelled design-hypotheses section (Phase B: LLM-generated, evidence-
  bound; until the generator lands this is a labelled placeholder).

Approval-token contract (v1): a non-blank ``design_approval_id`` is the caller's
explicit assertion that the design action was approved (the token is obtained via
the approval control plane's ``approve``). Cross-checking the token against the
control-plane store is a documented hardening follow-up — recorded here so it is
not mistaken for done.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

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
        an evidence-bound LLM generation (config-driven prompt)."""
        return (
            "## Design / optimization hypotheses (approved)\n\n"
            f"> Generated under approval `{approval_id}`. These are **hypotheses**, "
            f"not validated results — each must be confirmed experimentally.\n\n"
            "_Evidence-bound hypothesis generation is being wired in Phase B; this "
            "section confirms the approval gate opened and provenance is attached._"
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

        requested = control.get("requested_outputs") or "evidence_only"
        approval_id = control.get("design_approval_id")
        query = control.get("query") or ""

        # Emit {markdown, control_transfer?} for the terminal EnvelopeStep, which
        # shapes the WorkflowResult (and is the form run_workflow recognizes). A
        # control_transfer present → EnvelopeStep returns status=needs_input.
        if requested != _DESIGN:
            log.info("DesignGateStep %s: evidence_only → ok", self.name)
            return {"markdown": evidence_md}

        if _is_blank(approval_id):
            md = (
                f"{evidence_md.rstrip()}\n\n"
                "## Design / optimization output — WITHHELD\n\n"
                "> Design/optimization output was requested but no "
                "`design_approval_id` was provided. Approval is required and must be "
                "explicit. Obtain approval via the approval control plane (`approve`), "
                "then re-call with the returned `design_approval_id`. The evidence "
                "above is complete and unaffected."
            )
            ct = needs_prerequisite_transfer(
                "design_approval",
                message=(
                    "Design/optimization output requires explicit approval. Get a "
                    "design_approval_id via the approval control plane, then re-call."
                ),
            )
            log.info("DesignGateStep %s: design requested w/o approval → needs_input", self.name)
            return {"markdown": md, "control_transfer": ct.model_dump(mode="json")}

        design_md = self._design_section(query, evidence_md, str(approval_id))
        log.info(
            "DesignGateStep %s: design approved (%s) → ok with design section",
            self.name,
            approval_id,
        )
        return {"markdown": f"{evidence_md.rstrip()}\n\n{design_md}\n"}
