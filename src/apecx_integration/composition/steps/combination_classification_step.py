"""Classification step for the epitope-combination feasibility workflow.

Single responsibility: a pure transform. Given the normalized intake payload, computes
the readiness summary and the four deterministic classifications — epitope-level support,
structural placement, combination-level support, and immunodominance — and forwards them
to the release step. Forwards an upstream terminal payload (an intake miss) untouched.

Deterministic: no LLM call, no external service.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._combination_common import is_terminal, unwrap_single_key
from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_OUTPUT_KEY = "classify_output"


def _class_from_mapping(raw: Any) -> str:
    if isinstance(raw, dict):
        cls = raw.get("class") or raw.get("classification")
        if isinstance(cls, str) and cls.strip():
            return cls.strip().lower()
    return ""


class CombinationClassificationStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CombinationClassificationStep(BaseStep):
    COMPONENT_TYPE: str = "combination_classification_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    LLM_ROLE: str = "none"

    @classmethod
    def _get_config_class(cls):
        return CombinationClassificationStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CombinationClassificationStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        payload = unwrap_single_key(input_data)
        # Terminal pass-through: an upstream intake miss is already EnvelopeStep-shaped.
        # Forward it untouched so a downstream step never clobbers an early terminal.
        if is_terminal(payload):
            return {_OUTPUT_KEY: payload}

        self.emit_progress("classifying epitope combination evidence")
        evidence_parts = payload.get("evidence_parts") or {}
        epitopes = payload.get("epitopes") or []
        readiness = self._readiness(evidence_parts, payload.get("candidate_parts") or {}, epitopes)
        preliminary = self._preliminary_assessment(evidence_parts, epitopes)
        out = {**payload, "readiness": readiness, "preliminary": preliminary}
        append_stage_report(
            out,
            stage="combination_classification",
            order=2,
            markdown=(
                f"Combination-level support: {preliminary['combination_support']['classification']}; "
                f"structural placement: {preliminary['structural_placement']['classification']}; "
                f"immunodominance: {preliminary['immunodominance']['classification']}."
            ),
            data={"readiness": readiness},
        )
        return {_OUTPUT_KEY: out, "stage_reports": out["stage_reports"]}

    @staticmethod
    def _readiness(
        evidence_parts: dict[str, Any],
        candidate_parts: dict[str, Any],
        epitopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sr = evidence_parts.get("structural_reasoning")
        fv = evidence_parts.get("functional_validation")
        return {
            "candidate_released": bool(candidate_parts.get("candidate_released")),
            "epitope_count": len(epitopes),
            "epitopes_with_sequence": sum(1 for e in epitopes if e.get("sequence")),
            "epitopes_with_coordinates": sum(
                1 for e in epitopes if e.get("start") is not None and e.get("end") is not None
            ),
            "placement_context_available": bool(isinstance(sr, dict) and sr.get("available")),
            "annotation_context_available": bool(isinstance(fv, dict) and fv),
            "combination_evidence_records": len(evidence_parts.get("combination_evidence") or []),
            "immunodominance_evidence_records": len(
                evidence_parts.get("immunodominance_evidence")
                or evidence_parts.get("dominance_evidence")
                or []
            ),
        }

    def _preliminary_assessment(
        self, evidence_parts: dict[str, Any], epitopes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        epitope_support = [self._epitope_support(e) for e in epitopes]
        return {
            "epitope_support": epitope_support,
            "structural_placement": self._structural_placement(evidence_parts, epitopes),
            "combination_support": self._combination_support(evidence_parts, epitope_support),
            "immunodominance": self._immunodominance(evidence_parts, epitopes),
        }

    @staticmethod
    def _epitope_support(epitope: dict[str, Any]) -> dict[str, Any]:
        rec_class = _class_from_mapping(epitope.get("recognition_evidence"))
        if epitope.get("role") == "candidate":
            classification = "approved candidate peptide"
            basis = "released by the candidate-peptide assessment workflow"
        elif "direct" in rec_class:
            classification = "direct epitope-level support"
            basis = "epitope carries direct reported source support"
        elif epitope.get("recognition_evidence"):
            classification = "reported epitope-level support"
            basis = "epitope carries reported source support"
        elif epitope.get("start") is not None and epitope.get("end") is not None:
            classification = "location-only support"
            basis = "epitope has source coordinates but no reported support field"
        elif epitope.get("source") or epitope.get("evidence"):
            classification = "source-described support"
            basis = "epitope carries source metadata without coordinate-level support"
        else:
            classification = "insufficient evidence"
            basis = "epitope lacks source metadata, coordinates, and support evidence"
        return {
            "label": epitope["label"],
            "classification": classification,
            "basis": basis,
            "has_sequence": bool(epitope.get("sequence")),
            "has_coordinates": epitope.get("start") is not None and epitope.get("end") is not None,
        }

    @staticmethod
    def _structural_placement(
        evidence_parts: dict[str, Any], epitopes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        sr = evidence_parts.get("structural_reasoning")
        coord_epitopes = [
            e for e in epitopes if e.get("start") is not None and e.get("end") is not None
        ]
        mapped = 0
        if isinstance(sr, dict) and sr.get("available"):
            mapped_regions = sr.get("mapped_regions") or []
            for e in coord_epitopes:
                if any(
                    isinstance(r, dict)
                    and r.get("start") == e.get("start")
                    and r.get("end") == e.get("end")
                    for r in mapped_regions
                ):
                    mapped += 1
        if mapped == len(epitopes) and epitopes:
            classification = "common-reference support"
        elif len(coord_epitopes) == len(epitopes) and epitopes:
            classification = "coordinate-only support"
        elif coord_epitopes:
            classification = "partial coordinate support"
        else:
            classification = "insufficient placement basis"
        return {
            "classification": classification,
            "mapped_epitopes": mapped,
            "coordinate_epitopes": len(coord_epitopes),
            "epitope_count": len(epitopes),
            "layout_analysis": "not evaluated in this workflow",
        }

    @staticmethod
    def _combination_support(
        evidence_parts: dict[str, Any], epitope_support: list[dict[str, Any]]
    ) -> dict[str, Any]:
        combined = evidence_parts.get("combination_evidence") or evidence_parts.get(
            "multi_epitope_constructs"
        )
        if isinstance(combined, list) and combined:
            return {
                "classification": "direct combination-level support",
                "basis": "combination-level records are present in the evidence bundle",
                "record_count": len(combined),
            }
        if epitope_support and all(
            e["classification"]
            in {
                "approved candidate peptide",
                "direct epitope-level support",
                "reported epitope-level support",
            }
            for e in epitope_support
        ):
            return {
                "classification": "epitope-level support only",
                "basis": "epitope-level support exists, but combination-level behavior was not evaluated",
                "record_count": 0,
            }
        return {
            "classification": "insufficient evidence",
            "basis": "combination-level support and complete epitope-level support are absent",
            "record_count": 0,
        }

    @staticmethod
    def _immunodominance(
        evidence_parts: dict[str, Any], epitopes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        direct = evidence_parts.get("immunodominance_evidence") or evidence_parts.get(
            "dominance_evidence"
        )
        epitope_direct = [e for e in epitopes if e.get("immunodominance_evidence")]
        if isinstance(direct, list) and direct:
            return {
                "classification": "direct immunodominance records",
                "basis": "immunodominance records are present in the evidence bundle",
                "record_count": len(direct),
            }
        if epitope_direct:
            return {
                "classification": "epitope-level immunodominance metadata",
                "basis": "at least one epitope carries immunodominance metadata",
                "record_count": len(epitope_direct),
            }
        return {
            "classification": "not evaluated",
            "basis": "no immunodominance evidence was supplied",
            "record_count": 0,
        }


__all__ = ["CombinationClassificationStep", "CombinationClassificationStepConfig"]
