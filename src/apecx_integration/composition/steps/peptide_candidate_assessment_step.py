"""PeptideCandidateAssessmentStep - deterministic follow-up candidate assessment.

Consumes a structured evidence Bundle from a prior conserved-region workflow and emits a
neutral, approval-gated candidate assessment. The step is deterministic: no LLM call, no
network call, and no generated sequence outside the evidence bundle.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.handles.store import default_handle_store
from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.schemas.control_transfer import needs_prerequisite_transfer
from apecx_integration.composition.schemas.data_shapes import Bundle
from apecx_integration.composition.steps._proceed import render_how_to_proceed

log = logging.getLogger(__name__)

_INPUT_KEY = "assessment_input"
_OUTPUT_KEY = "assessment_output"
_APPROVAL_PREREQ = "candidate_sequence_approval"
_EVIDENCE_PREREQ = "conserved_region_evidence"


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _clean_sequence(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"[^A-Za-z]", "", v).upper()


def _as_fraction(v: Any, default: float = 0.0) -> float:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    if out > 1.0:
        out = out / 100.0
    return min(max(out, 0.0), 1.0)


def _fmt_score(v: Any) -> str:
    return f"{float(v or 0.0):.2f}"


def _region_key(region: dict[str, Any]) -> tuple[Any, Any]:
    return (region.get("start"), region.get("end"))


# Weight given to broad-effectiveness (pan-clade conservation) in the candidate score. The
# remaining 1 - _W_BREADTH is carved proportionally from the five existing components, so a
# single-clade virus (breadth not evaluated) keeps the exact pre-breadth ranking.
_W_BREADTH = 0.15


def _columns_in(regions: Any) -> set[int]:
    """Set of alignment-column indices covered by a list of inclusive ``[start, end]`` regions."""
    cols: set[int] = set()
    if isinstance(regions, list):
        for r in regions:
            start, end = (r.get("start"), r.get("end")) if isinstance(r, dict) else (None, None)
            if isinstance(start, int) and isinstance(end, int) and end >= start:
                cols.update(range(start, end + 1))
    return cols


def _breadth_phrase(b: Any) -> str:
    if not isinstance(b, dict) or b.get("classification") == "not evaluated":
        return "not evaluated (fewer than 2 clades or breadth unavailable)"
    return (
        f"{b['classification']} ({b.get('n_pan_columns', 0)}/{b.get('region_length', 0)} "
        f"columns identical across all clades, {_fmt_score(b.get('score'))})"
    )


class PeptideCandidateAssessmentStepConfig(StepConfig):
    """Config for PeptideCandidateAssessmentStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    min_candidate_length: int = Field(default=5, ge=1)
    preferred_min_length: int = Field(default=8, ge=1)
    preferred_max_length: int = Field(default=25, ge=1)
    max_candidate_length: int = Field(default=40, ge=1)
    max_alternates: int = Field(default=2, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_lengths(self) -> PeptideCandidateAssessmentStepConfig:
        if not (
            self.min_candidate_length
            <= self.preferred_min_length
            <= self.preferred_max_length
            <= self.max_candidate_length
        ):
            raise ValueError(
                "candidate length fields must satisfy min <= preferred_min <= preferred_max <= max"
            )
        return self


class PeptideCandidateAssessmentStep(BaseStep):
    COMPONENT_TYPE: str = "peptide_candidate_assessment_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    LLM_ROLE: str = "none"

    @classmethod
    def _get_config_class(cls):
        return PeptideCandidateAssessmentStepConfig

    @classmethod
    def extract_component_config(cls, config: PeptideCandidateAssessmentStepConfig) -> dict:
        base = super().extract_component_config(config)
        return {
            **base,
            "min_candidate_length": config.min_candidate_length,
            "preferred_min_length": config.preferred_min_length,
            "preferred_max_length": config.preferred_max_length,
            "max_candidate_length": config.max_candidate_length,
            "max_alternates": config.max_alternates,
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._min_len = int(component_config.get("min_candidate_length", 5))
        self._pref_min = int(component_config.get("preferred_min_length", 8))
        self._pref_max = int(component_config.get("preferred_max_length", 25))
        self._max_len = int(component_config.get("max_candidate_length", 40))
        self._max_alternates = int(component_config.get("max_alternates", 2))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"PeptideCandidateAssessmentStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "evidence_data_handle" not in input_data
            and "evidence_bundle" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        self.emit_progress("loading conserved-region evidence")
        parts, missing = self._load_evidence(input_data)
        if missing:
            return {_OUTPUT_KEY: self._needs_evidence()}

        readiness = self._readiness(parts)
        regions = self._candidate_regions(parts.get("conserved_regions"))
        if not regions:
            return {_OUTPUT_KEY: self._needs_conserved_regions(readiness, parts)}

        scope_query = self._scope_query(parts, input_data)
        protein = parts.get("protein")
        store = get_design_approval_store()
        approval_id = input_data.get("design_approval_id")
        ok, reason = store.validate(token=approval_id, query=scope_query, protein=protein)
        if not ok:
            token = self._approval_token(store, approval_id, reason, scope_query, protein)
            return {
                _OUTPUT_KEY: self._withheld_output(
                    readiness=readiness,
                    reason=reason,
                    token=token,
                    scope_query=scope_query,
                    protein=protein,
                )
            }

        self.emit_progress("ranking candidate regions")
        ranked = self._rank_candidates(regions, parts)
        if not ranked:
            return {_OUTPUT_KEY: self._needs_conserved_regions(readiness, parts)}
        primary = ranked[0]
        alternates = ranked[1 : 1 + self._max_alternates]
        output = self._approved_output(
            parts=parts,
            readiness=readiness,
            primary=primary,
            alternates=alternates,
            approval_id=str(approval_id),
            scope_query=scope_query,
            protein=protein,
        )
        return {_OUTPUT_KEY: output}

    def _load_evidence(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        handle = payload.get("evidence_data_handle")
        inline = payload.get("evidence_bundle")
        has_handle = isinstance(handle, str) and bool(handle.strip())
        has_inline = inline is not None
        if has_handle and has_inline:
            raise ValueError(
                "PeptideCandidateAssessmentStep: provide exactly one evidence source "
                "(evidence_data_handle OR evidence_bundle), not both."
            )
        if not has_handle and not has_inline:
            return {}, True
        if has_handle:
            shape = default_handle_store().get(str(handle).strip())
            if not isinstance(shape, Bundle):
                raise ValueError(
                    "PeptideCandidateAssessmentStep: evidence_data_handle must resolve to "
                    f"a Bundle DataShape, got {type(shape).__name__}."
                )
            return dict(shape.parts), False
        return self._bundle_parts(inline), False

    @staticmethod
    def _bundle_parts(raw: Any) -> dict[str, Any]:
        if isinstance(raw, Bundle):
            return dict(raw.parts)
        if not isinstance(raw, dict):
            raise ValueError(
                "PeptideCandidateAssessmentStep: evidence_bundle must be a Bundle dict "
                f"or parts dict, got {type(raw).__name__}."
            )
        if raw.get("kind") == "bundle":
            parts = raw.get("parts")
            if not isinstance(parts, dict):
                raise ValueError("PeptideCandidateAssessmentStep: bundle.parts must be a dict.")
            return dict(parts)
        return dict(raw)

    @staticmethod
    def _readiness(parts: dict[str, Any]) -> dict[str, Any]:
        structural_reasoning = parts.get("structural_reasoning")
        functional = parts.get("functional_validation")
        return {
            "conserved_regions": len(parts.get("conserved_regions") or []),
            "structural_records": len(parts.get("structural_records") or []),
            "publications": len(parts.get("publications") or []),
            "structural_reasoning_available": bool(
                isinstance(structural_reasoning, dict) and structural_reasoning.get("available")
            ),
            "functional_validation_available": bool(isinstance(functional, dict) and functional),
        }

    def _candidate_regions(self, raw_regions: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(raw_regions, list):
            return out
        for i, region in enumerate(raw_regions):
            if not isinstance(region, dict):
                continue
            sequence = _clean_sequence(region.get("consensus"))
            if not sequence:
                continue
            length = int(region.get("length") or len(sequence))
            if length < self._min_len or length > self._max_len:
                continue
            out.append({**region, "_index": i, "_sequence": sequence, "_length": length})
        return out

    @staticmethod
    def _scope_query(parts: dict[str, Any], payload: dict[str, Any]) -> str:
        question = payload.get("assessment_question")
        if isinstance(question, str) and question.strip():
            return question.strip()
        query = parts.get("query")
        if isinstance(query, str) and query.strip():
            return query.strip()
        return "conserved epitope candidate assessment"

    @staticmethod
    def _approval_token(store: Any, approval_id: Any, reason: str, query: str, protein: Any) -> str:
        if _is_blank(approval_id) or "unknown" in reason:
            return store.request(query=query, protein=protein)
        return str(approval_id)

    def _rank_candidates(
        self, regions: list[dict[str, Any]], parts: dict[str, Any]
    ) -> list[dict[str, Any]]:
        sr = parts.get("structural_reasoning") if isinstance(parts, dict) else None
        fv = parts.get("functional_validation") if isinstance(parts, dict) else None
        breadth_data = parts.get("cross_clade_breadth") if isinstance(parts, dict) else None
        # Run-level gate: when breadth was computed for this run, the discount applies to EVERY
        # candidate uniformly (one whose region lacks usable coords contributes 0 but is still
        # discounted) so scores stay comparable across candidates; a single-clade run (breadth
        # unavailable) keeps the exact pre-breadth formula. Gating per-candidate instead would let
        # a coord-less candidate keep an undiscounted base and outrank an evaluated pan-clade one.
        breadth_available = isinstance(breadth_data, dict) and bool(breadth_data.get("available"))
        scored = []
        for region in regions:
            exposure = self._exposure(region, sr)
            corroboration = self._corroboration(region, sr)
            recognition = self._recognition(region, exposure.get("mapped_residues") or [], fv)
            breadth = self._breadth(region, breadth_data)
            identity = _as_fraction(region.get("mean_identity", region.get("identity")))
            length_score = self._length_score(int(region["_length"]))
            base = (
                0.40 * identity
                + 0.15 * length_score
                + 0.20 * exposure["score"]
                + 0.15 * corroboration["score"]
                + 0.10 * recognition["score"]
            )
            # Broad-effectiveness is additive: blend it in when breadth was computed for the run;
            # otherwise the score is exactly the pre-breadth formula (no single-clade regression).
            if breadth_available:
                total = (1.0 - _W_BREADTH) * base + _W_BREADTH * breadth["score"]
            else:
                total = base
            scored.append(
                {
                    "sequence": region["_sequence"],
                    "source_region": {
                        "start": region.get("start"),
                        "end": region.get("end"),
                        "length": region["_length"],
                        "mean_identity": identity,
                    },
                    "score": round(total, 4),
                    "score_components": {
                        "conservation": round(identity, 4),
                        "length_suitability": round(length_score, 4),
                        "structural_exposure": round(exposure["score"], 4),
                        "cross_structure_support": round(corroboration["score"], 4),
                        "reported_recognition": round(recognition["score"], 4),
                        "broad_effectiveness": round(breadth["score"], 4),
                    },
                    "structural_exposure": exposure,
                    "cross_structure_support": corroboration,
                    "reported_recognition_evidence": recognition,
                    "cross_clade_breadth": breadth,
                }
            )
        return sorted(
            scored,
            key=lambda c: (
                -c["score"],
                c["source_region"]["length"],
                c["source_region"].get("start")
                if c["source_region"].get("start") is not None
                else 10**9,
            ),
        )

    def _length_score(self, length: int) -> float:
        if self._pref_min <= length <= self._pref_max:
            return 1.0
        if self._min_len <= length <= self._max_len:
            return 0.6
        return 0.0

    @staticmethod
    def _exposure(region: dict[str, Any], sr: Any) -> dict[str, Any]:
        if not isinstance(sr, dict) or not sr.get("available"):
            return {"score": 0.0, "classification": "not evaluated", "mapped_residues": []}
        target = _region_key(region)
        mapped = [
            r
            for r in (sr.get("mapped_regions") or [])
            if isinstance(r, dict) and _region_key(r) == target
        ]
        if not mapped:
            return {"score": 0.0, "classification": "not mapped", "mapped_residues": []}
        residues: list[int] = []
        for item in mapped:
            residues.extend(r for r in (item.get("residues") or []) if isinstance(r, int))
        residues = sorted(set(residues))
        if not residues:
            return {
                "score": 0.0,
                "classification": "mapped without residue list",
                "mapped_residues": [],
            }
        exposed = {
            e.get("resi")
            for e in (sr.get("exposed_residues") or [])
            if isinstance(e, dict) and isinstance(e.get("resi"), int)
        }
        n_exposed = len(set(residues) & exposed)
        score = n_exposed / len(residues)
        if score >= 0.75:
            label = "mostly exposed"
        elif score > 0.0:
            label = "partly exposed"
        else:
            label = "not exposed in mapped structure"
        return {
            "score": score,
            "classification": label,
            "mapped_residues": residues,
            "n_exposed_mapped_residues": n_exposed,
        }

    @staticmethod
    def _corroboration(region: dict[str, Any], sr: Any) -> dict[str, Any]:
        if not isinstance(sr, dict) or not sr.get("available"):
            return {"score": 0.0, "classification": "not evaluated"}
        rows = [
            r
            for r in (sr.get("corroborated_residues") or [])
            if isinstance(r, dict)
            and r.get("region_start") == region.get("start")
            and r.get("region_end") == region.get("end")
        ]
        if not rows:
            return {"score": 0.0, "classification": "not available"}
        n = len(rows)
        k = sum(1 for r in rows if r.get("corroborated"))
        score = k / n if n else 0.0
        return {
            "score": score,
            "classification": "corroborated" if k else "not corroborated",
            "n_corroborated": k,
            "n_positions": n,
        }

    @staticmethod
    def _recognition(region: dict[str, Any], residues: list[int], fv: Any) -> dict[str, Any]:
        if not isinstance(fv, dict):
            return {"class": "insufficient evidence", "score": 0.0, "basis": "no validation block"}
        coincidence_residues = {
            c.get("residue")
            for c in (fv.get("coincidences") or [])
            if isinstance(c, dict) and isinstance(c.get("residue"), int)
        }
        if residues and coincidence_residues and set(residues) & coincidence_residues:
            return {
                "class": "direct evidence",
                "score": 1.0,
                "basis": "reported residue-level overlap with the selected region",
            }
        if fv.get("residue_level_annotation_available") and coincidence_residues:
            return {
                "class": "indirect evidence",
                "score": 0.5,
                "basis": "reported residue-level evidence exists elsewhere in the analyzed context",
            }
        if region and (
            fv.get("candidate_source") in {"structural_exposed_conserved", "conserved_regions_only"}
        ):
            return {
                "class": "indirect evidence",
                "score": 0.35,
                "basis": "conserved-region evidence with no direct recognition overlap",
            }
        return {
            "class": "insufficient evidence",
            "score": 0.0,
            "basis": "no reported recognition support found in the evidence bundle",
        }

    @staticmethod
    def _breadth(region: dict[str, Any], breadth: Any) -> dict[str, Any]:
        """Pan-clade breadth for a candidate region (broad-effectiveness signal).

        Maps the candidate's alignment columns against the pan-clade / clade-restricted column
        sets from ``cross_clade_breadth`` (both computed on the SAME pooled alignment, so column
        coordinates are comparable). ``score`` is the fraction of the candidate's columns that are
        identically conserved across EVERY clade. Degrades loud (``not evaluated``) when breadth
        was not computed (<2 clades / unavailable) or the region lacks usable coordinates."""
        none = {
            "score": 0.0,
            "classification": "not evaluated",
            "n_pan_columns": 0,
            "n_clade_restricted_columns": 0,
            "region_length": 0,
        }
        if not isinstance(breadth, dict) or not breadth.get("available"):
            return none
        start, end = region.get("start"), region.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            return none
        pan_cols = _columns_in(breadth.get("pan_clade_regions"))
        restricted_cols = _columns_in(breadth.get("clade_restricted_regions"))
        length = end - start + 1
        n_pan = sum(1 for c in range(start, end + 1) if c in pan_cols)
        n_restricted = sum(1 for c in range(start, end + 1) if c in restricted_cols)
        score = n_pan / length if length else 0.0
        if score >= 0.75:
            label = "pan-clade"
        elif score > 0.0:
            label = "partly pan-clade"
        elif n_restricted > 0:
            label = "clade-divergent"
        else:
            label = "no clade-conserved overlap"
        return {
            "score": score,
            "classification": label,
            "n_pan_columns": n_pan,
            "n_clade_restricted_columns": n_restricted,
            "region_length": length,
        }

    def _needs_evidence(self) -> dict[str, Any]:
        message = (
            "Run the upstream conserved-epitope evidence workflow first, then provide its "
            "data_handle as evidence_data_handle. Inline Bundle input is also accepted."
        )
        ct = needs_prerequisite_transfer("evidence_data_handle", message=message)
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                "No structured evidence bundle was supplied.\n\n"
                "## Approval requirement\n\n"
                "No candidate sequence can be released until evidence is supplied and the "
                "candidate-output approval gate is satisfied.\n"
            ),
            "control_transfer": ct.model_dump(mode="json"),
        }

    def _needs_conserved_regions(
        self, readiness: dict[str, Any], parts: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        ct = needs_prerequisite_transfer(
            _EVIDENCE_PREREQ,
            message=(
                "The supplied evidence bundle does not contain usable conserved-region "
                "consensus evidence. Re-run the upstream evidence workflow with sequence "
                "conservation available, then re-call this assessment."
            ),
        )
        # Surface the upstream's how-to-proceed guidance (e.g. available proteins for a sparse
        # taxon) so the dead-end carries an actionable next step instead of just "absent".
        guidance = render_how_to_proceed({"proceed_notes": (parts or {}).get("proceed_notes")})
        guidance_md = f"\n{guidance}\n" if guidance else ""
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                f"Evidence bundle loaded, but usable conserved-region consensus records: "
                f"{readiness.get('conserved_regions', 0)}.\n\n"
                "## Approval requirement\n\n"
                "No candidate sequence is released because the prerequisite evidence is absent.\n"
                f"{guidance_md}"
            ),
            "data": {
                "kind": "bundle",
                "parts": {"readiness": readiness, "candidate_released": False},
            },
            "control_transfer": ct.model_dump(mode="json"),
        }

    def _withheld_output(
        self,
        *,
        readiness: dict[str, Any],
        reason: str,
        token: str,
        scope_query: str,
        protein: Any,
    ) -> dict[str, Any]:
        how = (
            f"Approve token `{token}` with the existing `approve_design` control, then re-call "
            "this workflow with the same evidence and design_approval_id."
        )
        ct = needs_prerequisite_transfer(
            _APPROVAL_PREREQ,
            message=f"Candidate sequence output requires explicit approval ({reason}). {how}",
        )
        return {
            "markdown": (
                "# Answer\n\n"
                "## Evidence readiness\n\n"
                f"Loaded conserved-region evidence: {readiness.get('conserved_regions', 0)} "
                f"region(s), {readiness.get('structural_records', 0)} structural record(s), "
                f"{readiness.get('publications', 0)} publication record(s).\n\n"
                "## Approval requirement\n\n"
                f"Candidate peptide sequence output is withheld: {reason}. {how}\n"
            ),
            "data": {
                "kind": "bundle",
                "parts": {
                    "candidate_released": False,
                    "readiness": readiness,
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
        parts: dict[str, Any],
        readiness: dict[str, Any],
        primary: dict[str, Any],
        alternates: list[dict[str, Any]],
        approval_id: str,
        scope_query: str,
        protein: Any,
    ) -> dict[str, Any]:
        validation_gaps = self._validation_gaps(primary, parts)
        context_notes = self._presentation_contexts()
        markdown = self._render_approved(
            primary=primary,
            alternates=alternates,
            readiness=readiness,
            validation_gaps=validation_gaps,
            context_notes=context_notes,
            approval_id=approval_id,
        )
        data = {
            "kind": "bundle",
            "parts": {
                "candidate_released": True,
                "candidate": primary,
                "alternates": alternates,
                "presentation_contexts": context_notes,
                "validation_gaps": validation_gaps,
                "readiness": readiness,
                "approval": {
                    "token": approval_id,
                    "scope_query": scope_query,
                    "protein": protein,
                    "status": "approved",
                },
            },
        }
        return {"markdown": markdown, "data": data}

    @staticmethod
    def _presentation_contexts() -> list[dict[str, str]]:
        return [
            {
                "context": "particulate display context",
                "assessment": (
                    "Representation would depend on spacing, accessibility, and carrier "
                    "compatibility. This workflow does not define a construct."
                ),
            },
            {
                "context": "RNA-expression context",
                "assessment": (
                    "Representation would require a separately evaluated coding sequence and "
                    "processing context. This workflow emits no nucleotide sequence."
                ),
            },
            {
                "context": "recombinant protein context",
                "assessment": (
                    "Representation would require separate evaluation of folding, stability, "
                    "and display of the candidate segment."
                ),
            },
        ]

    @staticmethod
    def _validation_gaps(primary: dict[str, Any], parts: dict[str, Any]) -> list[str]:
        gaps = [
            "Binding, safety, expression, stability, dosing, manufacturing, and regulatory questions were not evaluated in this workflow.",
            "No nucleotide sequence, codon usage, expression condition, or laboratory protocol is produced.",
        ]
        if primary["reported_recognition_evidence"]["class"] != "direct evidence":
            gaps.append(
                "No direct reported recognition overlap was found for the selected candidate."
            )
        if primary["structural_exposure"]["score"] == 0.0:
            gaps.append(
                "The selected region was not mapped to exposed residues in the structural reasoning block."
            )
        breadth = primary.get("cross_clade_breadth") or {}
        if breadth.get("classification") not in (None, "not evaluated", "pan-clade"):
            gaps.append(
                f"The selected candidate is not pan-clade conserved "
                f"({breadth.get('n_pan_columns', 0)}/{breadth.get('region_length', 0)} columns "
                "identical across all clades); it may not cover every clade."
            )
        if not parts.get("functional_validation"):
            gaps.append("No functional-validation block was present in the evidence bundle.")
        return gaps

    @staticmethod
    def _render_approved(
        *,
        primary: dict[str, Any],
        alternates: list[dict[str, Any]],
        readiness: dict[str, Any],
        validation_gaps: list[str],
        context_notes: list[dict[str, str]],
        approval_id: str,
    ) -> str:
        region = primary["source_region"]
        scores = primary["score_components"]
        alt_lines = [
            f"- `{a['sequence']}` from cols {a['source_region'].get('start')}-{a['source_region'].get('end')} "
            f"(score {_fmt_score(a['score'])})"
            for a in alternates
        ]
        alternates_md = "\n".join(alt_lines) if alt_lines else "- No alternate candidate selected."
        context_md = "\n".join(f"- **{c['context']}**: {c['assessment']}" for c in context_notes)
        gaps_md = "\n".join(f"- {g}" for g in validation_gaps)
        return (
            "# Answer\n\n"
            "A minimal consensus peptide candidate can be proposed from the conserved-region "
            "evidence under the approved candidate-output gate. This is a research-grade "
            "assessment, not a claim of validation.\n\n"
            "## Reasoning process\n\n"
            "- Evidence readiness: "
            f"{readiness.get('conserved_regions', 0)} conserved region(s), "
            f"{readiness.get('structural_records', 0)} structural record(s), "
            f"{readiness.get('publications', 0)} publication record(s).\n"
            "- Ranking used conservation, peptide-length suitability, structural exposure, "
            "cross-structure support, and reported recognition evidence when present.\n"
            f"- Approval provenance: `{approval_id}`.\n\n"
            "## Minimal consensus peptide candidate\n\n"
            f"- Candidate peptide: `{primary['sequence']}`\n"
            f"- Source coordinates: alignment cols {region.get('start')}-{region.get('end')} "
            f"(length {region.get('length')}, mean identity {_fmt_score(region.get('mean_identity'))}).\n"
            f"- Overall evidence score: {_fmt_score(primary['score'])}.\n\n"
            "## Evidence supporting the candidate\n\n"
            f"- Conservation: {_fmt_score(scores['conservation'])}.\n"
            f"- Length suitability: {_fmt_score(scores['length_suitability'])}.\n"
            f"- Structural exposure: {primary['structural_exposure']['classification']} "
            f"({_fmt_score(scores['structural_exposure'])}).\n"
            f"- Cross-structure support: {primary['cross_structure_support']['classification']} "
            f"({_fmt_score(scores['cross_structure_support'])}).\n"
            f"- Reported recognition evidence: {primary['reported_recognition_evidence']['class']} "
            f"({primary['reported_recognition_evidence']['basis']}).\n"
            f"- Cross-clade breadth: {_breadth_phrase(primary.get('cross_clade_breadth'))}.\n\n"
            "## Presentation-context considerations\n\n"
            f"{context_md}\n\n"
            "## Validation gaps\n\n"
            f"{gaps_md}\n\n"
            "## Sources and evidence\n\n"
            "- Source region: conserved-region consensus from the supplied evidence bundle.\n"
            f"- Structural evidence records counted: {readiness.get('structural_records', 0)}.\n"
            f"- Publication records counted: {readiness.get('publications', 0)}.\n"
            f"- Alternate candidates considered:\n{alternates_md}\n\n"
            "## Limitations\n\n"
            "This workflow performs deterministic candidate assessment from existing evidence "
            "only. It does not perform docking, FoldX, MHC prediction, expression-yield "
            "analysis, safety assessment, or regulatory assessment.\n"
        )


__all__ = ["PeptideCandidateAssessmentStep", "PeptideCandidateAssessmentStepConfig"]
