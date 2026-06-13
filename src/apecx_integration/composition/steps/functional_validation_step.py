"""FunctionalValidationStep — C3 cross-check (mandatory spec stage 3).

Sits AFTER the structural-reasoning stage and BEFORE the evidence-review synthesis.
It cross-checks the structure-derived candidate epitope residues (the solvent-exposed
conserved positions in ``bundle["structural_reasoning"]``) and the MSA-derived
conserved regions (``bundle["conserved_regions"]``) against any FUNCTIONAL /
IMMUNOLOGICAL annotation already assembled in the bundle (VIOLIN immunology/vaccine
mappings; BV-BRC genome features).

BRUTAL-HONESTY CONTRACT. The VIOLIN mappings assembled upstream are pathogen/vaccine
*ontology* mappings (``synonym_id`` / ``canonical_term``) and the BV-BRC records are
*genome-level* (``genome_id`` / ``genome_name``) — neither carries residue-level
functional coordinates. So for the current evidence sources this stage almost always
finds NO residue-level annotation to cross-reference, and its value is the EXPLICIT,
loud statement of the evidence basis: "candidate epitopes are sequence+structure-
derived only; N immunology mapping(s) + M genome(s) give context but no residue-level
functional coordinates." That statement is itself useful — it tells the scientist
exactly how far the evidence reaches. The step ALSO makes a best-effort scan for
residue-level position fields, so the moment a richer functional source is wired in
(epitope-position records, annotated features), it will surface real coincidences
without a code change. It NEVER fabricates a coincidence.

RELIABILITY (G127): this step NEVER raises on a content/shape issue — it would strand
the chain to ``review`` and silently empty the whole run. Every case passes the bundle
through with ``bundle["functional_validation"]`` set and a ``functional_validation``
stage report (order 4) appended. It raises ONLY on a broken wiring contract (non-dict
input).
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_INPUT_KEY = "functional_input"
_STAGE = "functional_validation"
_STAGE_ORDER = 4

# Keys that, when present and non-empty on an assembled record, denote residue-level
# functional/immunological annotation worth cross-referencing against candidate epitope
# residues. Matched case-insensitively. None of the current VIOLIN/BV-BRC shapes carry
# these — the scan is the forward-compatible seam for richer functional sources.
_RESIDUE_POSITION_KEYS = frozenset(
    {
        "position",
        "positions",
        "residue",
        "residues",
        "start",
        "end",
        "epitope_seq",
        "epitope_sequence",
        "location",
        "site",
    }
)


class FunctionalValidationStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class FunctionalValidationStep(BaseStep):
    COMPONENT_TYPE: str = "functional_validation_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return FunctionalValidationStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"FunctionalValidationStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({functional_input: bundle}); direct
        # callers (tests) pass the bundle raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)  # shallow copy; we add functional_validation + a report
        result, markdown = self._validate(bundle)
        bundle["functional_validation"] = result

        append_stage_report(
            bundle,
            stage=_STAGE,
            order=_STAGE_ORDER,
            markdown=markdown,
            data=result,
        )
        log.info(
            "FunctionalValidationStep %s: candidates=%d residue_level=%s violin=%d bvbrc=%d",
            self.name,
            result["n_candidate_epitope_residues"],
            result["residue_level_annotation_available"],
            result["n_immunology_mappings"],
            result["n_genome_features"],
        )
        return bundle

    def _validate(self, bundle: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Cross-check candidate epitope residues against assembled functional annotation.

        Returns ``(structured_result, markdown)``. Never raises — names the absence of
        functional annotation rather than fabricating a coincidence.
        """
        sr = bundle.get("structural_reasoning") or {}
        exposed = sr.get("exposed_residues") or [] if isinstance(sr, dict) else []
        candidate_resis = sorted(
            {
                e.get("resi")
                for e in exposed
                if isinstance(e, dict) and isinstance(e.get("resi"), int)
            }
        )
        regions = bundle.get("conserved_regions") or []
        violin = bundle.get("violin_mappings") or []
        bvbrc = bundle.get("bvbrc_genomes") or []

        if candidate_resis:
            candidate_source = "structural_exposed_conserved"
        elif regions:
            candidate_source = "conserved_regions_only"
        else:
            candidate_source = "none"

        annotations = self._scan_residue_annotations(violin) + self._scan_residue_annotations(bvbrc)
        residue_level = bool(annotations)

        coincidences: list[dict[str, Any]] = []
        if residue_level and candidate_resis:
            cand = set(candidate_resis)
            for ann in annotations:
                for pos in ann["positions"]:
                    if pos in cand:
                        coincidences.append({"residue": pos, "annotation": ann["source"]})

        result = {
            "n_candidate_epitope_residues": len(candidate_resis),
            "candidate_epitope_residues": candidate_resis[:50],
            "candidate_source": candidate_source,
            "n_conserved_regions": len(regions),
            "n_immunology_mappings": len(violin),
            "n_genome_features": len(bvbrc),
            "residue_level_annotation_available": residue_level,
            "coincidences": coincidences,
        }
        result["assessment"] = self._assessment(result, sr)
        return result, result["assessment"]

    @staticmethod
    def _scan_residue_annotations(records: Any) -> list[dict[str, Any]]:
        """Best-effort scan for residue-level functional annotation on assembled records.

        Returns a list of ``{"source", "fields", "positions"}`` for every record carrying
        a non-empty residue-position field. Empty for the current VIOLIN/BV-BRC shapes
        (pathogen/vaccine ontology + genome-level) — which is the honest, expected result.
        """
        out: list[dict[str, Any]] = []
        if not isinstance(records, list):
            return out
        for rec in records:
            if not isinstance(rec, dict):
                continue
            fields = {
                k: v
                for k, v in rec.items()
                if k.lower() in _RESIDUE_POSITION_KEYS and v not in (None, "", [], {})
            }
            if not fields:
                continue
            positions = sorted(FunctionalValidationStep._coerce_positions(fields))
            out.append(
                {
                    "source": rec.get("source")
                    or rec.get("synonym_id")
                    or rec.get("genome_id")
                    or "annotated record",
                    "fields": fields,
                    "positions": positions,
                }
            )
        return out

    @staticmethod
    def _coerce_positions(fields: dict[str, Any]) -> set[int]:
        positions: set[int] = set()
        for v in fields.values():
            for item in v if isinstance(v, list) else [v]:
                try:
                    positions.add(int(item))
                except (TypeError, ValueError):
                    continue
        return positions

    @staticmethod
    def _assessment(result: dict[str, Any], sr: dict[str, Any]) -> str:
        n_cand = result["n_candidate_epitope_residues"]
        n_violin = result["n_immunology_mappings"]
        n_bvbrc = result["n_genome_features"]
        context = (
            f"{n_violin} VIOLIN immunology/vaccine mapping(s) and {n_bvbrc} BV-BRC "
            f"genome record(s) provide immunological/genomic context for this pathogen"
        )

        if result["residue_level_annotation_available"]:
            coincidences = result["coincidences"]
            if not n_cand:
                return (
                    f"Residue-level functional annotation is present, but no structure-derived "
                    f"candidate epitope residues were available to cross-reference. {context}."
                )
            if coincidences:
                resi = ", ".join(str(c["residue"]) for c in coincidences[:20])
                return (
                    f"{len(coincidences)} of {n_cand} candidate epitope residue(s) COINCIDE with "
                    f"annotated functional/immunological position(s) (residues {resi}) — these are "
                    f"the strongest-supported candidate epitope residues. {context}."
                )
            return (
                f"None of the {n_cand} candidate epitope residue(s) coincide with the available "
                f"annotated functional position(s); the candidates remain sequence+structure-"
                f"derived. {context}."
            )

        # No residue-level functional annotation in the assembled evidence — name it LOUD.
        if not n_cand:
            note = sr.get("note") if isinstance(sr, dict) else None
            why = f" (structural reasoning unavailable: {note})" if note else ""
            return (
                f"No residue-level functional annotation is available, and no structure-derived "
                f"candidate epitope residues were produced{why}. Functional validation is limited "
                f"to noting context: {context}. Candidate epitopes, where present, are "
                f"sequence-conservation-derived only."
            )
        return (
            f"Functional annotation not available at residue resolution — the {n_cand} candidate "
            f"epitope residue(s) are sequence+structure-derived only (MSA conservation + "
            f"per-residue SASA), with no known immunogenic/functional residue annotation in the "
            f"assembled evidence to corroborate them. {context}, but carry no residue-level "
            f"functional coordinates to cross-reference. This names the evidence basis: the "
            f"candidate epitopes rest on conservation + solvent exposure, not on prior functional "
            f"characterization of these specific positions."
        )


__all__ = ["FunctionalValidationStep", "FunctionalValidationStepConfig"]
