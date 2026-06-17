"""RheaMuscleAlignStep — MSA via MUSCLE dispatched through the Rhea MCP server (EO-54b).

The production-path counterpart to ``LocalMafftAlignStep``: same wire interface, different
backend. Where ``LocalMafftAlignStep`` shells out to a local MAFFT binary, this step drives the
already-verified ``rhea_muscle_alignment`` workflow (FastaCollectionStep → RheaFileToolStep →
AlignmentReportStep) which runs real MUSCLE inside a Rhea-managed conda env over MCP.

This is the **pluggable-aligner substitution** the conserved-sites workflow offers: the caller
picks the aligner (local ``mafft`` vs Rhea ``muscle``) and the rest of the pipeline
(ConservationScoreStep / ConservationReportStep) is aligner-agnostic because both align steps
emit the SAME ``{"alignment": {alignment_fasta, n_sequences, alignment_length, aligner, ...}}``
shape. Design §8 (aligner substitution); the user's "not confined purely to MUSCLE" steer — here
realized as "MUSCLE is one of several interchangeable aligners".

Real backend, NO mocks, NO silent degradation: if the Rhea server is unreachable, the rhea
module is not importable, or the subworkflow yields no alignment, the step FAILS LOUD (it does
not fall back to a local aligner or a fabricated alignment — substitution is the caller's
explicit choice, surfaced honestly).

Input  (after trigger-envelope unwrap): ``{"fasta_text": "<unaligned FASTA>", ...}``.
Output: ``{"alignment": {alignment_fasta, n_sequences, alignment_length, aligner: "muscle", ...}}``.
Any ``taxon_id`` / ``protein`` present on the input are passed through for downstream context.

Requires the Rhea MCP server reachable at ``$RHEA_MCP_URL`` (or the YAML default) and the ``rhea``
package importable in this process — the same prerequisites the ``rhea_muscle_alignment`` catalog
entry declares. See docs/return_of_control_implementation_plan.md §"EO-54a — VERIFIED".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)

# The verified rhea muscle workflow lives next to the conserved-sites workflow, under
# composition/workflows/rhea_muscle_alignment/. Resolve it relative to this file so the step
# is location-independent (this file is at composition/steps/, the workflow at
# composition/workflows/rhea_muscle_alignment/workflow.yml).
_DEFAULT_RHEA_WORKFLOW_YAML = (
    Path(__file__).resolve().parent.parent / "workflows" / "rhea_muscle_alignment" / "workflow.yml"
)


class RheaMuscleAlignStepConfig(StepConfig):
    """Config for Rhea-backed MUSCLE alignment. A StepConfig subclass — no ``extra='forbid'``."""

    rhea_workflow_yaml: str = Field(
        default=str(_DEFAULT_RHEA_WORKFLOW_YAML),
        description="Path to the rhea_muscle_alignment workflow.yml this step drives.",
    )
    # The conda env build on the FIRST muscle run can take ~50s; allow generous headroom.
    timeout_seconds: float = Field(default=900.0, gt=0)
    settle_ms: int = Field(default=200, ge=0)


class RheaMuscleAlignStep(BaseStep):
    """Align an unaligned FASTA via MUSCLE over Rhea MCP; emit the LocalMafftAlignStep shape."""

    COMPONENT_TYPE = "rhea_muscle_align_step"

    @classmethod
    def _get_config_class(cls):
        return RheaMuscleAlignStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._rhea_workflow_yaml: str = getattr(
            config, "rhea_workflow_yaml", str(_DEFAULT_RHEA_WORKFLOW_YAML)
        )
        self._timeout: float = float(getattr(config, "timeout_seconds", 900.0))
        self._settle_ms: int = int(getattr(config, "settle_ms", 200))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        payload = self._unwrap(input_data)
        fasta_text = payload.get("fasta_text")
        if not isinstance(fasta_text, str) or not fasta_text.strip():
            raise ValueError(
                f"RheaMuscleAlignStep '{self.name}': input must carry a non-empty 'fasta_text' "
                f"string; got {type(fasta_text).__name__}"
            )
        if fasta_text.count(">") < 2:
            raise ValueError(
                f"RheaMuscleAlignStep '{self.name}': need ≥2 sequences to align; the input FASTA "
                f"has {fasta_text.count('>')}."
            )

        report_out = await self._drive_rhea_muscle(fasta_text)

        aligned = report_out.get("alignment_fasta")
        if not isinstance(aligned, str) or aligned.count(">") < 2:
            raise ValueError(
                f"RheaMuscleAlignStep '{self.name}': Rhea muscle subworkflow returned no usable "
                f"alignment (got {report_out!r}). The Rhea server may be unreachable or the "
                f"muscle tool failed — NOT falling back to a mock or a local aligner."
            )

        out: dict[str, Any] = {
            "alignment_fasta": aligned,
            "n_sequences": report_out.get("n_sequences", aligned.count(">")),
            "alignment_length": report_out.get("alignment_length"),
            "aligner": "muscle",
        }
        # Pass through identifying context + the per-strain disclosure fields for the downstream
        # report (parity with the MAFFT step's _with_live_context).
        for key in ("taxon_id", "protein", "records", "n_fetched", "n_dropped_length_outlier"):
            if key in payload:
                out[key] = payload[key]
        self.nb_logger.info(
            "RheaMuscleAlignStep %s: aligned %d sequences (length %s) via Rhea MUSCLE",
            self.name,
            out["n_sequences"],
            out["alignment_length"],
        )
        return {"alignment": out}

    async def _drive_rhea_muscle(self, fasta_text: str) -> dict[str, Any]:
        """Run the verified rhea_muscle_alignment subworkflow on ``fasta_text``.

        Reuses the EXACT verified pipeline + its DirectLinks (so the internal staging /
        MCP-dispatch / report plumbing is the wiring EO-54a green-verified). Nested
        ``Workflow.run`` is safe post-G115 (ContextVar workflow_id tagging)."""
        from nanobrain.core.workflow import Workflow

        wf = Workflow.from_config(self._rhea_workflow_yaml)
        await wf.initialize()

        # Robustness: honor an operator's $RHEA_MCP_URL over the YAML's baked-in default, so a
        # Rhea server on a non-standard host/port is reached (silent-failure guard — without
        # this the step would dispatch to localhost:3001 regardless).
        mcp_url = os.environ.get("RHEA_MCP_URL")
        if mcp_url:
            children = (
                getattr(wf, "child_steps", None)
                or getattr(wf, "_child_steps", None)
                or getattr(wf, "steps", None)
                or {}
            )
            muscle = children.get("muscle_alignment")
            if muscle is not None and hasattr(muscle, "_rfts_config"):
                muscle._rfts_config.mcp_url = mcp_url

        outputs = await wf.run(
            {"fasta_collection_input": {"fasta_text": fasta_text}},
            timeout=self._timeout,
            settle_ms=self._settle_ms,
        )
        report_out = outputs.get("workflow_output")
        if not isinstance(report_out, dict):
            raise ValueError(
                f"RheaMuscleAlignStep '{self.name}': rhea subworkflow produced no "
                f"'workflow_output' (got {type(report_out).__name__}). Is the Rhea server "
                f"reachable at {mcp_url or 'the YAML default'}?"
            )
        return report_out

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"RheaMuscleAlignStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if "fasta_text" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data
