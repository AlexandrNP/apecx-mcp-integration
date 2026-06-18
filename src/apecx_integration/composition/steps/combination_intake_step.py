"""Intake step for the epitope-combination feasibility workflow.

Single responsibility: load + validate inputs. Resolves the optional evidence Bundle and
the required candidate-peptide assessment Bundle (handle OR inline), verifies the candidate
is released, parses + validates the caller-supplied additional epitopes, assembles the
combined epitope list (candidate peptide + additional epitopes) under the cap, and computes
the approval ``scope_query`` + ``protein``. Emits a normalized intake payload for the
classification step, OR a terminal "needs_*" miss when a prerequisite is absent.

Deterministic: no LLM call, no external service.
"""

from __future__ import annotations

import logging
import re
from hashlib import sha256
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.handles.store import default_handle_store
from apecx_integration.composition.schemas.control_transfer import needs_prerequisite_transfer
from apecx_integration.composition.schemas.data_shapes import Bundle
from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_INPUT_KEY = "combination_request"
_OUTPUT_KEY = "intake_output"
_CANDIDATE_PREREQ = "approved_candidate_assessment"
_EPITOPE_PREREQ = "additional_epitopes"


def _clean_sequence(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"[^A-Za-z]", "", v).upper()


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _as_label(v: Any, fallback: str) -> str:
    if isinstance(v, str) and v.strip():
        return " ".join(v.strip().split())
    return fallback


class CombinationIntakeStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    max_epitopes: int = Field(default=6, ge=2)
    max_epitope_length: int = Field(default=80, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CombinationIntakeStep(BaseStep):
    COMPONENT_TYPE: str = "combination_intake_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    LLM_ROLE: str = "none"

    @classmethod
    def _get_config_class(cls):
        return CombinationIntakeStepConfig

    @classmethod
    def extract_component_config(cls, config: CombinationIntakeStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "max_epitopes": config.max_epitopes,
            "max_epitope_length": config.max_epitope_length,
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_epitopes = int(component_config.get("max_epitopes", 6))
        self._max_epitope_length = int(component_config.get("max_epitope_length", 80))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CombinationIntakeStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        payload = self._unwrap(input_data)

        self.emit_progress("loading candidate and evidence bundles")
        evidence_parts = self._load_optional_bundle(
            payload, handle_key="evidence_data_handle", inline_key="evidence_bundle"
        )
        candidate_parts, missing_candidate = self._load_required_bundle(
            payload,
            handle_key="candidate_assessment_handle",
            inline_key="candidate_assessment_bundle",
            label="candidate assessment",
        )
        if missing_candidate:
            return {_OUTPUT_KEY: self._needs_candidate()}
        if not self._candidate_is_released(candidate_parts):
            return {_OUTPUT_KEY: self._needs_released_candidate(candidate_parts)}

        additional, epitope_issue = self._additional_epitopes(payload.get("additional_epitopes"))
        if epitope_issue:
            return {_OUTPUT_KEY: self._needs_epitopes(epitope_issue)}

        candidate_epitope = self._candidate_epitope(candidate_parts)
        epitopes = [candidate_epitope, *additional]
        if len(epitopes) > self._max_epitopes:
            return {_OUTPUT_KEY: self._needs_epitopes("too many epitopes supplied")}

        scope_query = self._scope_query(payload, evidence_parts, candidate_parts, epitopes)
        protein = self._scope_protein(evidence_parts, candidate_parts)

        self.emit_progress("normalizing epitope combination intake")
        bundle = {
            "evidence_parts": evidence_parts,
            "candidate_parts": candidate_parts,
            "epitopes": epitopes,
            "scope_query": scope_query,
            "protein": protein,
            "design_approval_id": payload.get("design_approval_id"),
        }
        append_stage_report(
            bundle,
            stage="combination_intake",
            order=1,
            markdown=(
                f"Staged {len(epitopes)} epitope(s) for combination assessment: the approved "
                f"candidate peptide + {len(epitopes) - 1} additional epitope(s)."
            ),
            data={"epitope_count": len(epitopes)},
        )
        # ``stage_reports`` is surfaced at the top of the returned dict (alongside the output-DU
        # key) so the G37 step_complete event carries it for the desktop stream; it also rides
        # ``bundle`` downstream so classify/release accumulate onto the same list.
        return {_OUTPUT_KEY: bundle, "stage_reports": bundle["stage_reports"]}

    @staticmethod
    def _unwrap(input_data: dict[str, Any]) -> dict[str, Any]:
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "candidate_assessment_handle" not in input_data
            and "candidate_assessment_bundle" not in input_data
        ):
            return input_data[_INPUT_KEY]
        return input_data

    def _load_required_bundle(
        self,
        payload: dict[str, Any],
        *,
        handle_key: str,
        inline_key: str,
        label: str,
    ) -> tuple[dict[str, Any], bool]:
        handle = payload.get(handle_key)
        inline = payload.get(inline_key)
        has_handle = isinstance(handle, str) and bool(handle.strip())
        has_inline = inline is not None
        if has_handle and has_inline:
            raise ValueError(
                f"CombinationIntakeStep: provide exactly one {label} source "
                f"({handle_key} OR {inline_key}), not both."
            )
        if not has_handle and not has_inline:
            return {}, True
        return self._bundle_parts_from_source(handle if has_handle else inline), False

    def _load_optional_bundle(
        self,
        payload: dict[str, Any],
        *,
        handle_key: str,
        inline_key: str,
    ) -> dict[str, Any]:
        handle = payload.get(handle_key)
        inline = payload.get(inline_key)
        has_handle = isinstance(handle, str) and bool(handle.strip())
        has_inline = inline is not None
        if has_handle and has_inline:
            raise ValueError(
                f"CombinationIntakeStep: provide exactly one evidence source "
                f"({handle_key} OR {inline_key}), not both."
            )
        if not has_handle and not has_inline:
            return {}
        return self._bundle_parts_from_source(handle if has_handle else inline)

    @staticmethod
    def _bundle_parts_from_source(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            shape = default_handle_store().get(raw.strip())
            if not isinstance(shape, Bundle):
                raise ValueError(
                    "CombinationIntakeStep: handle must resolve to a Bundle "
                    f"DataShape, got {type(shape).__name__}."
                )
            return dict(shape.parts)
        if isinstance(raw, Bundle):
            return dict(raw.parts)
        if not isinstance(raw, dict):
            raise ValueError(
                "CombinationIntakeStep: bundle source must be a Bundle dict "
                f"or parts dict, got {type(raw).__name__}."
            )
        if raw.get("kind") == "bundle":
            parts = raw.get("parts")
            if not isinstance(parts, dict):
                raise ValueError("CombinationIntakeStep: bundle.parts must be a dict.")
            return dict(parts)
        return dict(raw)

    @staticmethod
    def _candidate_is_released(candidate_parts: dict[str, Any]) -> bool:
        return bool(candidate_parts.get("candidate_released") and candidate_parts.get("candidate"))

    def _additional_epitopes(self, raw: Any) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(raw, list) or not raw:
            return [], "additional_epitopes must be a non-empty list"
        out: list[dict[str, Any]] = []
        for i, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                return [], f"additional_epitopes[{i - 1}] must be an object"
            seq = _clean_sequence(item.get("sequence"))
            if seq and len(seq) > self._max_epitope_length:
                return [], f"epitope {i} sequence exceeds max_epitope_length"
            start = _as_int(item.get("start"))
            end = _as_int(item.get("end"))
            if start is None and isinstance(item.get("coordinates"), dict):
                start = _as_int(item["coordinates"].get("start"))
                end = _as_int(item["coordinates"].get("end"))
            if start is not None and end is not None and end < start:
                return [], f"epitope {i} has end before start"
            out.append(
                {
                    "role": "additional",
                    "label": _as_label(item.get("label") or item.get("name"), f"epitope {i}"),
                    "sequence": seq,
                    "start": start,
                    "end": end,
                    "source": item.get("source") or item.get("provenance"),
                    "recognition_evidence": item.get("reported_recognition_evidence")
                    or item.get("recognition_evidence"),
                    "immunodominance_evidence": item.get("immunodominance_evidence")
                    or item.get("dominance_evidence"),
                    "evidence": item.get("evidence"),
                }
            )
        return out, None

    @staticmethod
    def _candidate_epitope(candidate_parts: dict[str, Any]) -> dict[str, Any]:
        candidate = candidate_parts.get("candidate") if isinstance(candidate_parts, dict) else {}
        region = candidate.get("source_region") if isinstance(candidate, dict) else {}
        return {
            "role": "candidate",
            "label": "approved candidate peptide",
            "sequence": candidate.get("sequence") if isinstance(candidate, dict) else "",
            "start": region.get("start") if isinstance(region, dict) else None,
            "end": region.get("end") if isinstance(region, dict) else None,
            "source": "candidate_assessment",
            "recognition_evidence": candidate.get("reported_recognition_evidence")
            if isinstance(candidate, dict)
            else None,
            "immunodominance_evidence": None,
            "evidence": {
                "score": candidate.get("score") if isinstance(candidate, dict) else None,
                "score_components": candidate.get("score_components")
                if isinstance(candidate, dict)
                else None,
                "structural_exposure": candidate.get("structural_exposure")
                if isinstance(candidate, dict)
                else None,
                "cross_structure_support": candidate.get("cross_structure_support")
                if isinstance(candidate, dict)
                else None,
            },
        }

    @staticmethod
    def _scope_query(
        payload: dict[str, Any],
        evidence_parts: dict[str, Any],
        candidate_parts: dict[str, Any],
        epitopes: list[dict[str, Any]],
    ) -> str:
        question = payload.get("assessment_question")
        if isinstance(question, str) and question.strip():
            base = question.strip()
        else:
            query = evidence_parts.get("query") or candidate_parts.get("approval", {}).get(
                "scope_query"
            )
            base = query.strip() if isinstance(query, str) and query.strip() else ""
        if not base:
            base = "epitope combination feasibility assessment"
        fragments = [CombinationIntakeStep._epitope_scope_fragment(e) for e in epitopes]
        return f"{base} | epitope_scope: {';'.join(fragments)}"

    @staticmethod
    def _epitope_scope_fragment(epitope: dict[str, Any]) -> str:
        start = epitope.get("start")
        end = epitope.get("end")
        coords = f"{start}-{end}" if start is not None and end is not None else "no-coords"
        seq = _clean_sequence(epitope.get("sequence"))
        digest = sha256(seq.encode("ascii")).hexdigest()[:12] if seq else "no-seq"
        label = re.sub(r"[^A-Za-z0-9_.-]+", "_", epitope.get("label") or "epitope")
        role = re.sub(r"[^A-Za-z0-9_.-]+", "_", epitope.get("role") or "epitope")
        return f"{role}:{label}:{coords}:len{len(seq)}:sha256-{digest}"

    @staticmethod
    def _scope_protein(evidence_parts: dict[str, Any], candidate_parts: dict[str, Any]) -> Any:
        approval = candidate_parts.get("approval")
        if isinstance(approval, dict) and approval.get("protein") is not None:
            return approval.get("protein")
        return evidence_parts.get("protein")

    def _needs_candidate(self) -> dict[str, Any]:
        ct = needs_prerequisite_transfer(
            _CANDIDATE_PREREQ,
            message=(
                "Run the conserved_epitope_candidate_assessment workflow first, then provide "
                "its data_handle as candidate_assessment_handle."
            ),
        )
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                "No approved candidate-peptide assessment was supplied.\n\n"
                "## Approval requirement\n\n"
                "Run the candidate-peptide assessment first, then provide its data handle. "
                "No epitope combination can be assessed until a released candidate peptide "
                "is supplied.\n"
            ),
            "control_transfer": ct.model_dump(mode="json"),
        }

    def _needs_released_candidate(self, candidate_parts: dict[str, Any]) -> dict[str, Any]:
        ct = needs_prerequisite_transfer(
            _CANDIDATE_PREREQ,
            message="The supplied candidate-assessment bundle does not contain a released candidate peptide.",
        )
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                "The candidate-peptide assessment was loaded, but no released candidate "
                "peptide was found.\n\n"
                "## Approval requirement\n\n"
                "Re-run the candidate-peptide assessment with approval, then re-call this "
                "workflow.\n"
            ),
            "data": {
                "kind": "bundle",
                "parts": {
                    "combination_released": False,
                    "candidate_released": bool(candidate_parts.get("candidate_released")),
                },
            },
            "control_transfer": ct.model_dump(mode="json"),
        }

    def _needs_epitopes(self, reason: str) -> dict[str, Any]:
        ct = needs_prerequisite_transfer(_EPITOPE_PREREQ, message=reason)
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                f"The additional epitopes are not usable: {reason}.\n\n"
                "## Approval requirement\n\n"
                "Provide a non-empty list of usable additional epitopes.\n"
            ),
            "control_transfer": ct.model_dump(mode="json"),
        }


__all__ = ["CombinationIntakeStep", "CombinationIntakeStepConfig"]
