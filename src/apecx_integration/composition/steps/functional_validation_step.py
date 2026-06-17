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

from apecx_integration.agents.functional.iedb_client import IedbClient
from apecx_integration.agents.functional.residue_annotation import (
    cross_check_residues,
    gather_annotation_context,
)
from apecx_integration.agents.functional.sifts_client import SiftsClient
from apecx_integration.agents.functional.uniprot_client import UniProtClient
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
    # E3-3: cross-check candidate epitope residues against REAL residue-level annotation
    # (UniProt features + SIFTS numbering bridge + IEDB epitopes). Default ON in production;
    # offline unit tests set it false to stay hermetic (the real path has its own
    # network-gated integration tests).
    fetch_residue_annotations: bool = Field(default=True)
    timeout_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description="Wall-clock budget for the live SIFTS/UniProt/IEDB residue-annotation lookups.",
    )

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

    @classmethod
    def extract_component_config(cls, config: FunctionalValidationStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "fetch_residue_annotations": getattr(config, "fetch_residue_annotations", True),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._fetch_residue_annotations: bool = bool(
            component_config.get("fetch_residue_annotations", True)
        )

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

        self.emit_progress("validating functional annotations")

        bundle = dict(input_data)  # shallow copy; we add functional_validation + a report
        annotation = await self._fetch_real_annotation(bundle)
        self.emit_progress("cross-checking candidate residues against annotation")
        result, markdown = self._validate(bundle, annotation)
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

    async def _fetch_real_annotation(self, bundle: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch SIFTS+UniProt+IEDB residue-level annotation for the analysed structure.

        Returns ``None`` when the real path is disabled or there is no PDB/chain to look up
        (the step then falls back to the legacy VIOLIN/BV-BRC scan). Returns the annotation
        context dict otherwise — ``{available: True, ...}`` or ``{available: False, note}``.
        NEVER raises (G127): any network/wiring failure becomes a named ``available: False``.
        """
        if not self._fetch_residue_annotations:
            return None
        sr = bundle.get("structural_reasoning") or {}
        if not (
            isinstance(sr, dict)
            and sr.get("available")
            and isinstance(sr.get("pdb_id"), str)
            and isinstance(sr.get("chain"), str)
        ):
            return None
        pdb_id, chain = sr["pdb_id"], sr["chain"]
        try:
            async with (
                SiftsClient() as sifts,
                UniProtClient() as uniprot,
                IedbClient() as iedb,
            ):
                return await gather_annotation_context(
                    pdb_id, chain, sifts=sifts, uniprot=uniprot, iedb=iedb
                )
        except Exception as exc:  # noqa: BLE001 — degrade loud, never strand the cascade
            note = (
                f"Residue-level functional lookup for {pdb_id} chain {chain} failed "
                f"({type(exc).__name__}: {exc}); other evidence still synthesized."
            )
            log.warning("FunctionalValidationStep %s: %s", self.name, note)
            return {"available": False, "note": note}

    def _validate(
        self, bundle: dict[str, Any], annotation: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str]:
        """Cross-check candidate epitope residues against functional annotation.

        Two annotation sources, in priority order:

        1. REAL residue-level annotation (``annotation`` from SIFTS+UniProt+IEDB), when
           ``annotation["available"]`` — the E3-3 path. Emits rich per-residue coincidences
           + a complete per-residue ``residue_findings`` list (named absence per residue),
           so an "available" result is NEVER an empty coincidences with no named absence.
        2. The legacy VIOLIN/BV-BRC residue-position scan (forward-compat seam), used when
           the real path is off / unavailable.

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

        result = {
            "n_candidate_epitope_residues": len(candidate_resis),
            "candidate_epitope_residues": candidate_resis[:50],
            "candidate_source": candidate_source,
            "n_conserved_regions": len(regions),
            "n_immunology_mappings": len(violin),
            "n_genome_features": len(bvbrc),
        }

        real_available = bool(annotation and annotation.get("available"))
        annotation_note = annotation.get("note") if annotation else None

        if real_available:
            cross = cross_check_residues(candidate_resis, annotation)
            result.update(
                {
                    "residue_level_annotation_available": True,
                    "annotation_source": "UniProt+SIFTS+IEDB",
                    "coincidences": cross["coincidences"],
                    "residue_findings": cross["residue_findings"],
                    "uniprot_accessions": annotation.get("accessions", []),
                    "uniprot_release": annotation.get("uniprot_release"),
                    "query_date": annotation.get("query_date"),
                    "n_uniprot_features": annotation.get("n_uniprot_features", 0),
                    "n_iedb_epitope_spans": annotation.get("n_iedb_epitope_spans", 0),
                    "iedb_notes": annotation.get("iedb_notes", []),
                    "annotation_note": annotation_note,
                }
            )
            result["assessment"] = self._real_assessment(result)
            return result, result["assessment"]

        # Legacy VIOLIN/BV-BRC residue-position scan (real path off or unavailable).
        annotations = self._scan_residue_annotations(violin) + self._scan_residue_annotations(bvbrc)
        residue_level = bool(annotations)
        coincidences: list[dict[str, Any]] = []
        if residue_level and candidate_resis:
            cand = set(candidate_resis)
            for ann in annotations:
                for pos in ann["positions"]:
                    if pos in cand:
                        coincidences.append({"residue": pos, "annotation": ann["source"]})

        result.update(
            {
                "residue_level_annotation_available": residue_level,
                "annotation_source": "VIOLIN/BV-BRC scan" if residue_level else "none",
                "coincidences": coincidences,
                "residue_findings": [],
                "annotation_note": annotation_note,
            }
        )
        result["assessment"] = self._assessment(result, sr, annotation_note)
        return result, result["assessment"]

    @staticmethod
    def _real_assessment(result: dict[str, Any]) -> str:
        """Assessment for the REAL (UniProt+SIFTS+IEDB) annotation path."""
        n_cand = result["n_candidate_epitope_residues"]
        accs = ", ".join(result.get("uniprot_accessions") or []) or "the analysed chain"
        rel = result.get("uniprot_release") or "?"
        n_feat = result.get("n_uniprot_features", 0)
        n_iedb = result.get("n_iedb_epitope_spans", 0)
        coincidences = result["coincidences"]
        provenance = (
            f"Cross-checked against UniProt {accs} (release {rel}; {n_feat} residue "
            f"feature(s)) with the SIFTS author-numbering bridge, plus {n_iedb} IEDB "
            f"epitope span(s)."
        )
        if not n_cand:
            return (
                "Real residue-level functional annotation was retrieved, but no structure-"
                f"derived candidate epitope residues were available to cross-reference. {provenance}"
            )
        if coincidences:
            lines = "; ".join(
                c.get("type")
                and f"residue {c['residue']}→{c['accession']}:{c['unp_pos']} ({c['type']})"
                or f"residue {c['residue']}→{c['accession']}:{c['unp_pos']} (IEDB "
                f"{c.get('epitope')})"
                for c in coincidences[:20]
            )
            return (
                f"{len(coincidences)} of {n_cand} candidate epitope residue(s) COINCIDE with "
                f"REAL residue-level functional/immunological annotation ({lines}) — these are "
                f"the strongest-supported candidate epitope residues. {provenance}"
            )
        return (
            f"None of the {n_cand} candidate epitope residue(s) coincide with the REAL "
            f"residue-level annotation; each is explicitly named as having no functional/"
            f"immunological feature (see residue_findings). The candidates remain sequence+"
            f"structure-derived. {provenance}"
        )

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
    def _assessment(
        result: dict[str, Any], sr: dict[str, Any], annotation_note: str | None = None
    ) -> str:
        n_cand = result["n_candidate_epitope_residues"]
        n_violin = result["n_immunology_mappings"]
        n_bvbrc = result["n_genome_features"]
        # When the REAL residue-level lookup was attempted but degraded (no UniProt xref /
        # network down), name the reason loud rather than silently omitting it (CC-1/G127).
        degrade = f" Residue-level lookup degraded: {annotation_note}" if annotation_note else ""
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
                f"sequence-conservation-derived only.{degrade}"
            )
        return (
            f"Functional annotation not available at residue resolution — the {n_cand} candidate "
            f"epitope residue(s) are sequence+structure-derived only (MSA conservation + "
            f"per-residue SASA), with no known immunogenic/functional residue annotation in the "
            f"assembled evidence to corroborate them. {context}, but carry no residue-level "
            f"functional coordinates to cross-reference. This names the evidence basis: the "
            f"candidate epitopes rest on conservation + solvent exposure, not on prior functional "
            f"characterization of these specific positions.{degrade}"
        )


__all__ = ["FunctionalValidationStep", "FunctionalValidationStepConfig"]
