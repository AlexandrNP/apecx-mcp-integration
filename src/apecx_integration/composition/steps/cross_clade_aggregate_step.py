"""CrossCladeAggregateStep — broad-effectiveness breadth across clades (Req 5).

Sits AFTER ``clade_grouping`` (and, when present, after the per-clade re-analysis map). For each
conserved region it computes, on the SHARED full alignment, how many clades conserve that region
— the broad-effectiveness signal: a region conserved across ALL clades is a broad-spectrum
epitope candidate; one conserved in only some is clade-restricted.

Coordinate-correctness: the breadth is computed on the POOLED alignment (every clade scored on
the SAME columns) — NOT by comparing independently-re-aligned per-clade coordinates, which would
be incomparable. The per-clade re-alignment (``clade_results`` from the MapSubworkflowStep, when
present) is folded in only as supplementary per-clade region COUNTS.

RELIABILITY (G127): never raises on a content issue; raises ONLY on non-dict input. With <2
clades it emits a loud "not applicable" and passes through.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._clade_grouping import clade_conservation_breadth
from apecx_integration.composition.steps._stage_report import append_stage_report
from apecx_integration.composition.steps.conservation_score_step import _parse_fasta

log = logging.getLogger(__name__)

_INPUT_KEY = "cross_clade_input"
_STAGE = "cross_clade_breadth"
_STAGE_ORDER = 6


class CrossCladeAggregateStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    identity_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="A region is 'conserved within a clade' when its mean per-column identity "
        "across that clade's sequences is at least this.",
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CrossCladeAggregateStep(BaseStep):
    COMPONENT_TYPE: str = "cross_clade_aggregate_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CrossCladeAggregateStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._identity_threshold: float = float(getattr(config, "identity_threshold", 0.9))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CrossCladeAggregateStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)
        clade_groups = bundle.get("clade_groups") or []
        alignment_fasta = bundle.get("alignment_fasta")

        if len(clade_groups) < 2 or not isinstance(alignment_fasta, str):
            grouping = bundle.get("clade_grouping") or {}
            note = (
                grouping.get("note")
                or "fewer than 2 clades — broad-effectiveness breadth not applicable"
            )
            # Carry the EVIDENCE for the verdict (identity distribution + adaptive-probe subgroup
            # count) so the synthesis can show WHY breadth was not assessed, not just assert it.
            breadth = {"available": False, "note": note}
            if "identity_distribution" in grouping:
                breadth["identity_distribution"] = grouping["identity_distribution"]
            if "n_subgroups_at_098" in grouping:
                breadth["n_subgroups_at_098"] = grouping["n_subgroups_at_098"]
            bundle["cross_clade_breadth"] = breadth
            append_stage_report(
                bundle,
                stage=_STAGE,
                order=_STAGE_ORDER,
                markdown=f"Cross-clade breadth: {note}.",
                data=dict(breadth),
            )
            return bundle

        self.emit_progress(f"computing epitope breadth across {len(clade_groups)} clades")
        aligned = _parse_fasta(alignment_fasta)
        member_ids = [g.get("member_ids") or [] for g in clade_groups]
        breadth = clade_conservation_breadth(
            aligned, member_ids, identity_threshold=self._identity_threshold
        )

        # Supplementary per-clade region counts from the (optional) per-clade re-analysis map.
        clade_results = bundle.get("clade_results") or []
        breadth["per_clade_region_counts"] = (
            self._per_clade_counts(clade_results) if clade_results else None
        )
        bundle["cross_clade_breadth"] = breadth

        n_pan = len(breadth.get("pan_clade_regions") or [])
        n_restricted = len(breadth.get("clade_restricted_regions") or [])
        md = (
            f"Across {len(clade_groups)} clades: {n_pan} PAN-CLADE conserved region(s) "
            f"(broad-spectrum epitope candidates — same sequence conserved in EVERY clade) and "
            f"{n_restricted} clade-restricted region(s) (conserved within clades but divergent "
            f"between them — would not cover all strains)."
        )
        append_stage_report(
            bundle,
            stage=_STAGE,
            order=_STAGE_ORDER,
            markdown=md,
            data={
                "available": True,
                "n_clades": len(clade_groups),
                "n_pan_clade_regions": n_pan,
                "n_clade_restricted_regions": n_restricted,
            },
        )
        log.info("CrossCladeAggregateStep %s: %s", self.name, md)
        return bundle

    @staticmethod
    def _per_clade_counts(clade_results: list[Any]) -> list[dict[str, Any]]:
        """Fold the MapSubworkflowStep per-clade reports into {clade, n_conserved_regions}.

        Degrade-loud per item: a failed clade run (``_map_item_error``) is recorded as a named
        error entry, never silently dropped."""
        out: list[dict[str, Any]] = []
        for i, res in enumerate(clade_results):
            if not isinstance(res, dict):
                out.append({"clade": i, "error": "non-dict result"})
                continue
            if "_map_item_error" in res:
                out.append({"clade": i, "error": res["_map_item_error"]})
                continue
            parts = (res.get("data") or {}).get("parts") or {}
            regions = parts.get("conserved_regions") or []
            out.append({"clade": i, "n_conserved_regions": len(regions)})
        return out


__all__ = ["CrossCladeAggregateStep", "CrossCladeAggregateStepConfig"]
