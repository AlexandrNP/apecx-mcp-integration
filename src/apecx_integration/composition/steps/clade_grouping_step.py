"""CladeGroupingStep — group the aligned strains into clades for broad-effectiveness analysis.

Sits AFTER ``merge`` (which threads ``alignment_fasta`` + ``sequence_used_records`` onto the
bundle) and BEFORE the cross-clade aggregate. BV-BRC features carry no lineage/genotype field,
so clades are inferred by IDENTITY-CLUSTERING the already-aligned sequences (no metadata, no
phylogenetics tool). A homogeneous protein collapses to a single clade — an honest "broad-
effectiveness N/A" signal, not a failure.

Emits onto the bundle:
  - ``clade_groups``: ``[{"clade_id", "member_ids", "size"}]`` (metadata for the aggregate/render)
  - ``clade_fastas``: ``[fasta_text]`` in the SAME order (the list a MapSubworkflowStep re-aligns
    per clade — the per-clade looped execution layer; built from the ORIGINAL ungapped sequences)
  - ``clade_grouping``: a small summary

RELIABILITY (G127 + no-silent-failure): never raises on a content/shape issue (it would strand
the chain downstream). Raises ONLY on a broken wiring contract (non-dict input). When there are
<2 clades, ``clade_groups``/``clade_fastas`` are set to a single-group (or empty) with a LOUD
note — downstream renders "homogeneous; per-clade breadth N/A".
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._clade_grouping import cluster_by_identity
from apecx_integration.composition.steps._stage_report import append_stage_report
from apecx_integration.composition.steps.conservation_score_step import _parse_fasta

log = logging.getLogger(__name__)

_INPUT_KEY = "clade_grouping_input"
_STAGE = "clade_grouping"
_STAGE_ORDER = 5  # after sequence(1)/structural(2)/reasoning(3)/functional(4)


class CladeGroupingStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    clade_identity_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Two aligned sequences join the same clade when their pairwise identity "
        "(over shared non-gap columns) is at least this. Lower → fewer, broader clades.",
    )
    min_clade_size: int = Field(
        default=2,
        ge=2,
        description="Minimum sequences for a clade (conservation needs >=2). Smaller groups are "
        "reported as ungrouped outliers, never silently dropped.",
    )
    max_clades: int = Field(
        default=6,
        ge=1,
        description="Cap on clades fed to the per-clade re-analysis loop (bounds runtime). "
        "Excess (smallest) clades are folded into an 'others' note, not silently dropped.",
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CladeGroupingStep(BaseStep):
    COMPONENT_TYPE: str = "clade_grouping_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CladeGroupingStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._clade_identity_threshold: float = float(
            getattr(config, "clade_identity_threshold", 0.95)
        )
        self._min_clade_size: int = int(getattr(config, "min_clade_size", 2))
        self._max_clades: int = int(getattr(config, "max_clades", 6))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CladeGroupingStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)
        alignment_fasta = bundle.get("alignment_fasta")
        records = bundle.get("sequence_used_records") or []

        if not isinstance(alignment_fasta, str) or alignment_fasta.count(">") < 2:
            return self._degrade(
                bundle,
                "sequence conservation unavailable (no alignment) — per-clade breadth not applicable",
            )

        self.emit_progress("grouping strains into clades")
        aligned = _parse_fasta(alignment_fasta)  # [(id, gapped_seq)]
        clustering = cluster_by_identity(
            aligned,
            threshold=self._clade_identity_threshold,
            min_size=self._min_clade_size,
        )
        clades: list[list[str]] = clustering["clades"]
        ungrouped: list[str] = clustering["ungrouped"]

        if len(clades) < 2:
            return self._degrade(
                bundle,
                f"strains are homogeneous at the {self._clade_identity_threshold:.0%} identity "
                f"threshold ({len(clades)} clade) — per-clade breadth not applicable; the "
                f"sequence-conservation across all strains already reflects effectiveness",
                n_clades=len(clades),
            )

        # Cap clades (bound the per-clade loop runtime); fold the excess into a note.
        capped = clades[: self._max_clades]
        dropped_clades = clades[self._max_clades :]
        orig_by_id = {
            r.get("id"): r.get("sequence") for r in records if isinstance(r, dict) and r.get("id")
        }

        clade_groups: list[dict[str, Any]] = []
        clade_fastas: list[str] = []
        for ci, member_ids in enumerate(capped):
            # Build the clade FASTA from the ORIGINAL (ungapped) sequences for re-alignment;
            # fall back to the gapped alignment row (gaps stripped) when an id is missing.
            gapped = dict(aligned)
            lines = []
            for mid in member_ids:
                seq = orig_by_id.get(mid) or (gapped.get(mid, "").replace("-", ""))
                if seq:
                    lines.append(f">{mid}\n{seq}\n")
            clade_groups.append({"clade_id": ci, "member_ids": member_ids, "size": len(member_ids)})
            clade_fastas.append("".join(lines))

        bundle["clade_groups"] = clade_groups
        bundle["clade_fastas"] = clade_fastas
        summary = {
            "n_clades": len(capped),
            "clade_sizes": [len(ids) for ids in capped],
            "n_ungrouped": len(ungrouped),
            "n_clades_dropped_over_cap": len(dropped_clades),
            "identity_threshold": self._clade_identity_threshold,
        }
        bundle["clade_grouping"] = summary

        md = (
            f"Grouped the aligned strains into {len(capped)} clade(s) "
            f"(sizes {', '.join(str(len(ids)) for ids in capped)}; ≥{self._clade_identity_threshold:.0%} "
            f"identity)"
            + (f"; {len(ungrouped)} ungrouped outlier(s)" if ungrouped else "")
            + (
                f"; {len(dropped_clades)} smaller clade(s) beyond the cap not re-analyzed"
                if dropped_clades
                else ""
            )
            + "."
        )
        append_stage_report(bundle, stage=_STAGE, order=_STAGE_ORDER, markdown=md, data=summary)
        log.info("CladeGroupingStep %s: %s", self.name, md)
        return bundle

    def _degrade(self, bundle: dict[str, Any], note: str, n_clades: int = 0) -> dict[str, Any]:
        """Loud single-group/empty fallback — never raise, never silently empty."""
        bundle["clade_groups"] = []
        bundle["clade_fastas"] = []
        bundle["clade_grouping"] = {"n_clades": n_clades, "note": note}
        append_stage_report(
            bundle,
            stage=_STAGE,
            order=_STAGE_ORDER,
            markdown=f"Per-clade grouping: {note}.",
            data={"n_clades": n_clades, "note": note},
        )
        log.info("CladeGroupingStep %s: degrade — %s", self.name, note)
        return bundle


__all__ = ["CladeGroupingStep", "CladeGroupingStepConfig"]
