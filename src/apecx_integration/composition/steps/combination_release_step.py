"""Release step for the epitope-combination feasibility workflow.

Single responsibility: the design-approval gate. Validates the approval token against the
design-approval store for the exact request scope; without a valid approval it emits a
withheld output (epitope sequences stripped, a fresh approval token returned), and with one
it emits the approved combination assessment (markdown + data bundle). Forwards an upstream
terminal payload (an intake miss) untouched.

Deterministic: no LLM call, no external service.
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
from apecx_integration.composition.steps._combination_common import is_terminal, unwrap_single_key
from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_OUTPUT_KEY = "release_output"
_APPROVAL_PREREQ = "combination_assessment_approval"


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


class CombinationReleaseStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CombinationReleaseStep(BaseStep):
    COMPONENT_TYPE: str = "combination_release_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    LLM_ROLE: str = "none"

    @classmethod
    def _get_config_class(cls):
        return CombinationReleaseStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CombinationReleaseStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        payload = unwrap_single_key(input_data)
        # Terminal pass-through: an upstream intake miss is already EnvelopeStep-shaped.
        if is_terminal(payload):
            return {_OUTPUT_KEY: payload}

        epitopes = payload.get("epitopes") or []
        evidence_parts = payload.get("evidence_parts") or {}
        candidate_parts = payload.get("candidate_parts") or {}
        readiness = payload.get("readiness") or {}
        preliminary = payload.get("preliminary") or {}
        scope_query = payload.get("scope_query") or ""
        protein = payload.get("protein")
        approval_id = payload.get("design_approval_id")

        ok, reason = get_design_approval_store().validate(
            token=approval_id, query=scope_query, protein=protein
        )
        if not ok:
            token = self._approval_token(approval_id, reason, scope_query, protein)
            terminal = self._withheld_output(
                readiness=readiness,
                preliminary=preliminary,
                reason=reason,
                token=token,
                scope_query=scope_query,
                protein=protein,
                epitopes=epitopes,
            )
            stage_md = f"Combination output withheld pending design approval: {reason}."
        else:
            self.emit_progress("releasing approved epitope combination assessment")
            terminal = self._approved_output(
                readiness=readiness,
                preliminary=preliminary,
                evidence_parts=evidence_parts,
                candidate_parts=candidate_parts,
                epitopes=epitopes,
                approval_id=str(approval_id),
                scope_query=scope_query,
                protein=protein,
            )
            stage_md = "Released the approved epitope-combination assessment."

        # Surface the cumulative stage list (accumulated intake->classify->here) at the top of
        # the returned dict so the G37 step_complete event carries this stage for the desktop
        # stream. ``terminal`` itself stays EnvelopeStep-shaped (markdown/data/control_transfer).
        carrier = {"stage_reports": list(payload.get("stage_reports") or [])}
        append_stage_report(
            carrier, stage="combination_release", order=3, markdown=stage_md, data={}
        )
        return {_OUTPUT_KEY: terminal, "stage_reports": carrier["stage_reports"]}

    @staticmethod
    def _approval_token(approval_id: Any, reason: str, query: str, protein: Any) -> str:
        if _is_blank(approval_id) or "unknown" in reason:
            return get_design_approval_store().request(query=query, protein=protein)
        return str(approval_id)

    def _withheld_output(
        self,
        *,
        readiness: dict[str, Any],
        preliminary: dict[str, Any],
        reason: str,
        token: str,
        scope_query: str,
        protein: Any,
        epitopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ct = needs_prerequisite_transfer(
            _APPROVAL_PREREQ,
            message=(
                "Approve the returned design_approval_id, then re-run this workflow to release "
                "the epitope-level combination output."
            ),
        )
        safe_epitopes = self._epitope_records(epitopes, include_sequence=False)
        markdown = (
            "# Answer\n\n"
            "## Evidence readiness\n\n"
            "The candidate peptide and submitted epitopes were loaded. "
            f"Epitope count: {readiness.get('epitope_count', 0)}.\n\n"
            "## Approval requirement\n\n"
            f"Detailed combination output is withheld: {reason}. "
            f"Use design_approval_id `{token}` for this exact request after approval.\n\n"
            "## Validation gaps\n\n"
            "Only high-level availability is shown before approval. Epitope sequences, "
            "combined sequences, epitope order, linkers, and construct-design instructions are "
            "not released in this state.\n"
        )
        return {
            "markdown": markdown,
            "data": {
                "kind": "bundle",
                "parts": {
                    "combination_released": False,
                    "readiness": readiness,
                    "preliminary": preliminary,
                    "epitopes": safe_epitopes,
                    "approval": {
                        "required": True,
                        "token": token,
                        "scope_query": scope_query,
                        "protein": protein,
                    },
                },
            },
            "control_transfer": ct.model_dump(mode="json"),
        }

    def _approved_output(
        self,
        *,
        readiness: dict[str, Any],
        preliminary: dict[str, Any],
        evidence_parts: dict[str, Any],
        candidate_parts: dict[str, Any],
        epitopes: list[dict[str, Any]],
        approval_id: str,
        scope_query: str,
        protein: Any,
    ) -> dict[str, Any]:
        validation_gaps = self._validation_gaps(preliminary)
        epitope_records = self._epitope_records(epitopes, include_sequence=True)
        markdown = self._render_approved(
            readiness=readiness,
            preliminary=preliminary,
            validation_gaps=validation_gaps,
            epitopes=epitopes,
            evidence_parts=evidence_parts,
            candidate_parts=candidate_parts,
        )
        return {
            "markdown": markdown,
            "data": {
                "kind": "bundle",
                "parts": {
                    "combination_released": True,
                    "epitopes": epitope_records,
                    "epitope_support": preliminary["epitope_support"],
                    "structural_placement": preliminary["structural_placement"],
                    "combination_support": preliminary["combination_support"],
                    "immunodominance": preliminary["immunodominance"],
                    "validation_gaps": validation_gaps,
                    "readiness": readiness,
                    "approval": {
                        "required": True,
                        "token": approval_id,
                        "scope_query": scope_query,
                        "protein": protein,
                    },
                },
            },
        }

    @staticmethod
    def _validation_gaps(preliminary: dict[str, Any]) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = [
            {
                "item": "combination-level behavior",
                "status": preliminary["combination_support"]["classification"],
                "note": "combination-level behavior was not established by this workflow",
            },
            {
                "item": "epitope order and layout",
                "status": preliminary["structural_placement"]["classification"],
                "note": "layout, ordering, and linker analysis were not performed",
            },
            {
                "item": "relative immunodominance",
                "status": preliminary["immunodominance"]["classification"],
                "note": "relative immunodominance behavior was not established by this workflow",
            },
        ]
        gaps.extend(
            [
                {
                    "item": "external validation",
                    "status": "not evaluated in this workflow",
                    "note": "external validation was not performed",
                },
                {
                    "item": "construct readiness",
                    "status": "not evaluated in this workflow",
                    "note": "construct readiness was not evaluated",
                },
                {
                    "item": "operational use",
                    "status": "not evaluated in this workflow",
                    "note": "operational use was not evaluated",
                },
            ]
        )
        return gaps

    @staticmethod
    def _epitope_records(
        epitopes: list[dict[str, Any]], *, include_sequence: bool
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for e in epitopes:
            record: dict[str, Any] = {
                "label": e["label"],
                "role": e["role"],
                "start": e.get("start"),
                "end": e.get("end"),
                "source": e.get("source"),
                "has_sequence": bool(e.get("sequence")),
                "has_coordinates": e.get("start") is not None and e.get("end") is not None,
            }
            if include_sequence:
                record["sequence"] = e.get("sequence") or ""
            records.append(record)
        return records

    def _render_approved(
        self,
        *,
        readiness: dict[str, Any],
        preliminary: dict[str, Any],
        validation_gaps: list[dict[str, str]],
        epitopes: list[dict[str, Any]],
        evidence_parts: dict[str, Any],
        candidate_parts: dict[str, Any],
    ) -> str:
        epitope_lines = "\n".join(
            self._epitope_line(e) for e in self._epitope_records(epitopes, include_sequence=True)
        )
        evidence_lines = "\n".join(
            f"- {e['label']}: {e['classification']} ({e['basis']})."
            for e in preliminary["epitope_support"]
        )
        gap_lines = "\n".join(
            f"- {g['item']}: {g['status']}; {g['note']}." for g in validation_gaps
        )
        source_lines = self._source_lines(evidence_parts, candidate_parts)
        markdown = (
            "# Answer\n\n"
            "## Summary\n\n"
            "The supplied epitopes were organized as an evidence-limited epitope combination. "
            "This workflow does not establish combination-level behavior or readiness for use.\n\n"
            "## Epitopes\n\n"
            f"{epitope_lines}\n\n"
            "No combined sequence, epitope order, linker, or construct-design instructions are "
            "produced.\n\n"
            "## Evidence\n\n"
            f"{evidence_lines}\n\n"
            f"- Combination-level support: {preliminary['combination_support']['classification']}.\n"
            f"- Structural placement: {preliminary['structural_placement']['classification']}.\n"
            f"- Immunodominance considerations: {preliminary['immunodominance']['classification']}.\n\n"
            "## Validation gaps\n\n"
            f"{gap_lines}\n\n"
            "## Sources and evidence\n\n"
            f"{source_lines}\n\n"
            "## Limitations\n\n"
            "This is a deterministic catalog assessment over supplied records. It is not an "
            "external validation, construct-design plan, or operational determination.\n"
        )
        return markdown

    @staticmethod
    def _epitope_line(record: dict[str, Any]) -> str:
        coords = ""
        if record.get("start") is not None and record.get("end") is not None:
            coords = f", coordinates {record['start']}-{record['end']}"
        seq = record.get("sequence") or "not supplied"
        return (
            f"- {record['label']}: {seq}{coords}; source: {record.get('source') or 'not supplied'}."
        )

    @staticmethod
    def _source_lines(evidence_parts: dict[str, Any], candidate_parts: dict[str, Any]) -> str:
        approval = candidate_parts.get("approval")
        has_candidate_approval = isinstance(approval, dict) and bool(approval.get("token"))
        lines = [
            f"- Candidate peptide loaded: {bool(candidate_parts)}.",
            f"- Candidate approval metadata present: {has_candidate_approval}.",
            f"- Upstream record bundle supplied: {bool(evidence_parts)}.",
        ]
        return "\n".join(lines)


__all__ = ["CombinationReleaseStep", "CombinationReleaseStepConfig"]
