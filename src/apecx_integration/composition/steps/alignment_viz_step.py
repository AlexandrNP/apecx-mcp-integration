"""AlignmentVizStep — render the sequence-conservation visualization (E-viz).

Sits AFTER ``merge`` (which threads ``per_column_conservation`` / ``conserved_regions`` /
``alignment_fasta`` onto the bundle) and BEFORE the structural ``reasoning`` step. It is a
pure, side-effect-light step: it renders a conservation PNG artifact (matplotlib, optional)
and ALWAYS a text conservation track, stashes both on the bundle for the disclosure section,
and appends a stage report. Plotting is kept OFF the timeout-tight ``review`` LLM step.

RELIABILITY (G127 + no-silent-failure): never raises on a content/shape issue (it would
strand the chain to ``reasoning`` and silently empty the whole run). It raises ONLY on a
broken wiring contract (non-dict input). The PNG render degrades LOUD to the text track when
matplotlib is absent or the data is missing — the report always carries *some* visualization.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._alignment_viz import (
    _artifacts_dir,
    render_conservation_png,
    render_conservation_text,
)
from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_INPUT_KEY = "align_viz_input"
_STAGE = "alignment_viz"
_STAGE_ORDER = 1  # sits with sequence_conservation (order 1); inserted after it


class AlignmentVizStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class AlignmentVizStep(BaseStep):
    COMPONENT_TYPE: str = "alignment_viz_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return AlignmentVizStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"AlignmentVizStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({align_viz_input: bundle}); direct callers
        # (tests) pass the bundle raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)  # passthrough copy; we add alignment_viz_* + a report

        per_column = bundle.get("per_column_conservation")
        regions = bundle.get("conserved_regions") or []
        alignment_fasta = bundle.get("alignment_fasta")
        protein = str(bundle.get("protein") or "protein")
        summary = bundle.get("sequence_fetch_summary") or {}
        n_used = summary.get("n_used")

        # Always produce the text track (the degrade-loud floor).
        bundle["alignment_viz_text"] = render_conservation_text(
            per_column, regions, protein=protein, n_sequences=n_used
        )

        # Content-address key by taxon+protein; the figure/fasta basenames append a data digest.
        taxon = bundle.get("taxon_id")
        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{taxon}_{protein}").strip("_") or "conservation"

        # Stash the raw MAFFT alignment as a durable NATIVE artifact (content-addressed by the
        # alignment bytes) so the per-run folder carries the actual tool output, not just the plot.
        # Only the lightweight BASENAME rides the bundle → the durable handle; the MCP-layer gather
        # moves the file into <run_id>/tool_outputs/alignment.fasta. Independent of whether conserved
        # regions were found. Best-effort: a write failure never strands the chain.
        if alignment_fasta:
            try:
                fa_digest = hashlib.sha256(alignment_fasta.encode("utf-8", "ignore")).hexdigest()[
                    :10
                ]
                fa_path = _artifacts_dir() / f"alignment_{safe}_{fa_digest}.fasta"
                fa_path.write_text(alignment_fasta, encoding="utf-8")
                bundle["alignment_fasta_artifact"] = fa_path.name
            except Exception as exc:  # noqa: BLE001 — raw-artifact write is best-effort
                log.warning(
                    "AlignmentVizStep %s: alignment fasta write failed (%s: %s).",
                    self.name,
                    type(exc).__name__,
                    exc,
                )

        # Best-effort PNG (None when matplotlib absent / no data / render error).
        artifact = None
        if regions:
            self.emit_progress(f"rendering conservation visualization ({len(regions)} regions)")
            # CONTENT-ADDRESS the PNG by the alignment (+ regions) it plots, NOT just taxon+protein:
            # two runs of the same virus+protein can align a DIFFERENT strain subset, and the
            # report .md is run_id-keyed. A non-content basename would let an older report embed a
            # newer run's plot (a silent figure/prose mismatch). A content hash means identical
            # data reuses one file and any data change gets a distinct file — no stale mismatch.
            digest_src = (
                (alignment_fasta or "")
                + "|"
                + repr([(r.get("start"), r.get("end"), r.get("consensus")) for r in regions])
            )
            digest = hashlib.sha256(digest_src.encode("utf-8", "ignore")).hexdigest()[:10]
            artifact = render_conservation_png(
                per_column,
                regions,
                alignment_fasta,
                protein=protein,
                n_sequences=n_used,
                basename=f"conservation_{safe}_{digest}",
            )
        bundle["alignment_viz_artifact"] = artifact

        if regions:
            md = (
                f"Rendered the sequence-conservation visualization for {protein} "
                f"({len(regions)} conserved region(s))"
                + (
                    f" → `{artifact}`."
                    if artifact
                    else " as an inline text track (PNG unavailable — see report)."
                )
            )
        else:
            md = "No conserved regions to visualize (sequence conservation unavailable)."
        append_stage_report(
            bundle,
            stage=_STAGE,
            order=_STAGE_ORDER,
            markdown=md,
            data={
                "has_png": bool(artifact),
                "n_regions": len(regions),
            },
        )
        log.info(
            "AlignmentVizStep %s: %d region(s), png=%s",
            self.name,
            len(regions),
            artifact or "none",
        )
        return bundle


__all__ = ["AlignmentVizStep", "AlignmentVizStepConfig"]
