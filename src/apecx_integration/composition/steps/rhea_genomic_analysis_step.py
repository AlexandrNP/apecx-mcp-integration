"""RheaGenomicAnalysisStep — large-scale sequence-conservation analysis via RHEA.

The RHEA-backed genomic-analysis leg of ``viral_epitope_analysis``. Unlike the
digest (which only ranks/keeps top-N records), this step performs REAL analysis:
it fetches a representative subset of per-strain protein sequences from BV-BRC,
runs a genuine MUSCLE multiple-sequence alignment on the RHEA MCP server (Parsl-
distributed Galaxy tool), and computes per-column conservation — yielding the
antigenically-conserved regions of the target protein across strains.

It is ADDITIVE to the existing local-MAFFT sequence leg: that leg still runs;
this one adds a larger-scale, distributed analysis under a distinct bundle key.

Pattern: mirrors ``StructuralEvidenceStep`` — a linear-chain step that reads the
bundle, does its own analysis, EXTENDS the bundle with new keys, and passes it
through. Like its siblings it is DEGRADE-LOUD: a missing taxon/protein, an
unreachable RHEA server, or the ``rhea`` module not being importable becomes a
NAMED note on the bundle (never a silent empty result, never a raise) so the
downstream review always fires.

Input contract (the bundle, after trigger-envelope unwrap)::

    {"query": str, "taxon_id": int, "protein": str, ...}

Output: the same bundle, plus::

    {..bundle.., "rhea_conservation": {markdown, conserved_regions, n_sequences,
                                       alignment_length, aligner} | None,
                 "rhea_conservation_note": str | None}
"""

from __future__ import annotations

import logging
import os
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "rhea_genomic_input"
_RHEA_CORE_BUILDER = (
    "apecx_integration.composition.workflows.viral_conserved_sites.builder"
    ".build_viral_conserved_sites_rhea_core_workflow"
)


def _find_nested(d: Any, key: str, depth: int = 0) -> Any:
    """Best-effort search for ``key`` anywhere in a nested dict (report shape varies)."""
    if depth > 6 or not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    for v in d.values():
        found = _find_nested(v, key, depth + 1)
        if found is not None:
            return found
    return None


class RheaGenomicAnalysisStepConfig(StepConfig):
    """Config for RheaGenomicAnalysisStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    timeout_seconds: float = Field(
        default=900.0,
        description="Wall-clock budget for the nested RHEA conserved-sites run.",
    )
    settle_ms: int = Field(default=800, description="Cascade settle window for the inner run.")


class RheaGenomicAnalysisStep(BaseStep):
    COMPONENT_TYPE: str = "rhea_genomic_analysis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return RheaGenomicAnalysisStepConfig

    @classmethod
    def extract_component_config(cls, config: RheaGenomicAnalysisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "timeout_seconds": getattr(config, "timeout_seconds", 900.0),
            "settle_ms": getattr(config, "settle_ms", 800),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._timeout: float = float(component_config.get("timeout_seconds", 900.0))
        self._settle_ms: int = int(component_config.get("settle_ms", 800))

    def _params_unusable(self, bundle: dict[str, Any]) -> str | None:
        """Return a loud reason when the bundle can't feed the RHEA fetch, else None."""
        taxon_id = bundle.get("taxon_id")
        protein = bundle.get("protein")
        if not (isinstance(taxon_id, int) or (isinstance(taxon_id, str) and taxon_id.isdigit())):
            return (
                "no usable NCBI taxon_id on the query — RHEA genomic analysis needs an explicit "
                f"taxon_id (resolve the virus via harmonized_search); got {taxon_id!r}"
            )
        if not (isinstance(protein, str) and protein.strip()):
            return (
                "no protein/antigen name on the query — RHEA genomic analysis needs a protein to "
                f"fetch per-strain sequences (e.g. 'E1', 'structural polyprotein'); got {protein!r}"
            )
        return None

    async def _drive_rhea_conservation(self, taxon_id: Any, protein: str) -> dict[str, Any]:
        """Run the RHEA-MUSCLE conserved-sites inner workflow; return its report dict."""
        import importlib

        mod_path, _, fn_name = _RHEA_CORE_BUILDER.rpartition(".")
        builder = getattr(importlib.import_module(mod_path), fn_name)
        wf = builder()  # loaded Workflow (no-arg); RheaMuscleAlignStep imports rhea lazily
        await wf.initialize()
        outputs = await wf.run(
            {"fetch_in": {"taxon_id": int(taxon_id), "protein": protein}},
            timeout=self._timeout,
            settle_ms=self._settle_ms,
        )
        report = outputs.get("workflow_output")
        if not isinstance(report, dict):
            raise ValueError(
                "RHEA conserved-sites subworkflow produced no 'workflow_output' "
                f"(got {type(report).__name__}); is the Rhea server reachable at "
                f"{os.environ.get('RHEA_MCP_URL', 'the default localhost:3001')}?"
            )
        return report

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"RheaGenomicAnalysisStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        self.emit_progress("starting RHEA genomic analysis")

        bundle = dict(input_data)  # shallow copy; we add rhea_conservation
        bundle["rhea_conservation"] = None

        # MANDATORY leg (fail-closed): the RHEA large-scale MUSCLE conservation is required —
        # a result without it is meaningless. There is NO MAFFT-only fallback / degrade-to-note.
        # An unusable upstream (no taxon/protein) or a RHEA runtime failure RAISES; the prereq
        # gate (catalog ``requires: rhea``) refuses the run up front when RHEA is not configured.
        reason = self._params_unusable(bundle)
        if reason is not None:
            raise ValueError(
                f"RheaGenomicAnalysisStep '{self.name}': cannot run the MANDATORY RHEA "
                f"genomic-analysis leg — {reason}. RHEA is required (run `apecx-setup rhea`); "
                "there is no MAFFT-only fallback."
            )
        try:
            self.emit_progress("dispatching MUSCLE alignment")
            report = await self._drive_rhea_conservation(
                bundle["taxon_id"], str(bundle["protein"]).strip()
            )
            self.emit_progress("MUSCLE alignment complete")
            regions = _find_nested(report, "conserved_regions")
            bundle["rhea_conservation"] = {
                "markdown": _find_nested(report, "markdown"),
                "conserved_regions": regions if isinstance(regions, list) else [],
                "n_sequences": _find_nested(report, "n_sequences"),
                "alignment_length": _find_nested(report, "alignment_length"),
                "aligner": "muscle",
            }
            log.info(
                "RheaGenomicAnalysisStep %s: RHEA MUSCLE aligned %s sequences → %s conserved "
                "region(s)",
                self.name,
                bundle["rhea_conservation"]["n_sequences"],
                len(bundle["rhea_conservation"]["conserved_regions"]),
            )
        except Exception as exc:
            raise RuntimeError(
                f"RheaGenomicAnalysisStep '{self.name}': the MANDATORY RHEA genomic-analysis "
                f"leg failed ({type(exc).__name__}): {exc}. RHEA must be running "
                "(run `apecx-setup rhea`); there is no MAFFT-only fallback."
            ) from exc

        bundle["rhea_conservation_note"] = None

        from apecx_integration.composition.steps._stage_report import append_stage_report

        rc = bundle["rhea_conservation"]
        append_stage_report(
            bundle,
            stage="rhea_genomic_analysis",
            order=6,
            markdown=(
                f"RHEA MUSCLE aligned {rc.get('n_sequences')} sequences "
                f"(length {rc.get('alignment_length')}) → "
                f"{len(rc.get('conserved_regions') or [])} conserved region(s)."
            ),
            data={
                "n_sequences": rc.get("n_sequences"),
                "n_conserved_regions": len(rc.get("conserved_regions") or []),
            },
        )
        return bundle


__all__ = ["RheaGenomicAnalysisStep", "RheaGenomicAnalysisStepConfig"]
