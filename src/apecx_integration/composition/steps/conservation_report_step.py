"""ConservationReportStep — render a conservation result as markdown + a handle-able Bundle.

The presentation step between ``ConservationScoreStep`` (structured conservation) and
``EnvelopeStep`` (the §5 WorkflowResult). It emits ``{"markdown": ..., "data": {kind:
"bundle", ...}}`` so the EnvelopeStep puts the human summary on the markdown channel and the
full structured conservation result behind a data handle (kept out of the LLM context).

Generic: it works from the conservation result alone (sequence counts, conserved sites +
regions), so no per-step context threading is required.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)


class ConservationReportStepConfig(StepConfig):
    """Config — a StepConfig subclass, so NO ``extra='forbid'``."""

    max_regions_shown: int = Field(default=12, ge=1)


class ConservationReportStep(BaseStep):
    """Format a conservation result into markdown + a Bundle data payload."""

    COMPONENT_TYPE = "conservation_report_step"

    @classmethod
    def _get_config_class(cls):
        return ConservationReportStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_regions: int = int(getattr(config, "max_regions_shown", 12))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        cons = self._unwrap(input_data)
        for required in ("n_sequences", "alignment_length", "conserved_regions"):
            if required not in cons:
                raise ValueError(
                    f"ConservationReportStep '{self.name}': input is not a conservation result "
                    f"(missing {required!r}); got keys {sorted(cons.keys())}"
                )

        n_seq = cons["n_sequences"]
        aln_len = cons["alignment_length"]
        threshold = cons.get("conservation_threshold", 0.9)
        sites = cons.get("conserved_sites", [])
        regions = cons.get("conserved_regions", [])
        mean_identity = cons.get("mean_identity", 0.0)

        md = [
            "# Conserved sites",
            "",
            f"Aligned **{n_seq}** sequences (alignment length {aln_len}). Found "
            f"**{len(sites)}** conserved columns (≥{threshold:.0%} identity) across "
            f"**{len(regions)}** region(s). Mean per-column identity: {mean_identity:.2f}.",
        ]
        if regions:
            top = sorted(regions, key=lambda r: r["length"], reverse=True)[: self._max_regions]
            md += ["", f"**Top conserved regions** (alignment coordinates, of {len(regions)}):"]
            for r in top:
                motif = r["consensus"]
                shown = motif if len(motif) <= 40 else motif[:37] + "..."
                md.append(
                    f"- cols {r['start']}–{r['end']} (len {r['length']}): "
                    f"`{shown}` — mean identity {r.get('mean_identity', 0.0):.2f}"
                )
        else:
            md += ["", "_No region met the conservation threshold._"]

        markdown = "\n".join(md)
        self.nb_logger.info(
            "ConservationReportStep %s: report for %d seqs, %d regions",
            self.name,
            n_seq,
            len(regions),
        )
        return {
            "report": {
                "markdown": markdown,
                "data": {"kind": "bundle", "parts": cons},
            }
        }

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"ConservationReportStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # The conservation result always carries 'alignment_length'; if it's not at the top
        # level, descend into a single-key trigger envelope.
        if "alignment_length" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data


__all__ = ["ConservationReportStep", "ConservationReportStepConfig"]
