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
    max_sequences: int | None = Field(
        default=None,
        ge=1,
        description=(
            "BV-BRC per-strain subset the RHEA MUSCLE alignment fetches. None (default) = "
            "locus-aware: DESKTOP/MCP mode uses the reduced MAFFT-matching subset "
            "(DEFAULT_MAX_SEQUENCES=25), AGENT/HPC mode uses the larger Parsl-distributed subset "
            "(RHEA_AGENT_MAX_SEQUENCES=60). Set an int to pin it regardless of locus."
        ),
    )


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
            "max_sequences": getattr(config, "max_sequences", None),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._timeout: float = float(component_config.get("timeout_seconds", 900.0))
        self._settle_ms: int = int(component_config.get("settle_ms", 800))
        cfg_max = component_config.get("max_sequences", None)
        self._max_sequences: int | None = int(cfg_max) if cfg_max is not None else None

    def _effective_max_sequences(self) -> int:
        """The BV-BRC subset size to align. An explicit config value wins; else it is
        locus-aware — DESKTOP/MCP mode reduces RHEA to the SAME subset the local MAFFT leg
        uses (``DEFAULT_MAX_SEQUENCES``), while AGENT/HPC mode keeps the larger Parsl-
        distributed subset (``RHEA_AGENT_MAX_SEQUENCES``). Resolved per-run so a server
        started with ``--locus agent`` gets the full workload without reconfiguring the step."""
        if self._max_sequences is not None:
            return self._max_sequences
        from apecx_integration.composition.runtime.execution_locus import (
            ExecutionLocus,
            get_active_locus,
        )
        from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
            DEFAULT_MAX_SEQUENCES,
            RHEA_AGENT_MAX_SEQUENCES,
        )

        if get_active_locus() == ExecutionLocus.DESKTOP:
            return DEFAULT_MAX_SEQUENCES
        return RHEA_AGENT_MAX_SEQUENCES

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
        max_sequences = self._effective_max_sequences()
        self.emit_progress(f"aligning up to {max_sequences} sequences (locus-bounded)")
        # RheaMuscleAlignStep imports rhea lazily; max_sequences bounds the inner BV-BRC fetch.
        wf = builder(max_sequences=max_sequences)
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

        bundle = dict(input_data)  # shallow copy; we add rhea_conservation[_note]
        bundle["rhea_conservation"] = None
        note: str | None = None
        # The diagnosed cause when the RHEA call raises (None on the params-unusable path); feeds
        # the proceed_notes "why" so it names the true problem, not a generic "call failed".
        failure_cause: str | None = None

        # RHEA genomic-analysis is a MANDATORY part of the analysis — always attempted, always
        # DISCLOSED in the output. But its absence DEGRADES LOUD, it does NOT fail the run: the
        # rest of the end-to-end analysis (MAFFT sequence conservation, structural, literature)
        # still has merit. When RHEA can't run we emit a prominent warning + fix instructions
        # (here as the note + a proceed_notes "how to proceed" entry) and carry on.
        reason = self._params_unusable(bundle)
        if reason is not None:
            note = self._unavailable_warning(reason)
            log.warning("RheaGenomicAnalysisStep %s: %s", self.name, note)
        else:
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
            except Exception as exc:  # noqa: BLE001 — degrade-loud is the contract (do NOT fail)
                # G127: Workflow.run SWALLOWS the inner step's exception, so `exc` here is a
                # generic "no workflow_output" ValueError — NOT the real ModuleNotFoundError
                # (rhea client not importable) or connection error. Blaming "server unreachable"
                # then sends the operator to the wrong fix. Probe the actual prerequisites.
                failure_cause = await self._diagnose_rhea_failure(exc)
                note = self._unavailable_warning(failure_cause)
                log.warning("RheaGenomicAnalysisStep %s: %s", self.name, note)

        bundle["rhea_conservation_note"] = note

        from apecx_integration.composition.steps._stage_report import append_stage_report

        rc = bundle.get("rhea_conservation") or {}
        append_stage_report(
            bundle,
            stage="rhea_genomic_analysis",
            order=6,
            markdown=(
                note
                if note
                else (
                    f"RHEA MUSCLE aligned {rc.get('n_sequences')} sequences "
                    f"(length {rc.get('alignment_length')}) → "
                    f"{len(rc.get('conserved_regions') or [])} conserved region(s)."
                )
            ),
            data={
                "n_sequences": rc.get("n_sequences"),
                "n_conserved_regions": len(rc.get("conserved_regions") or []),
                "note": note,
            },
        )
        # Loud "how to proceed" guidance so the unavailability + fix surfaces in the report,
        # not just a buried field.
        if note:
            notes = list(bundle.get("proceed_notes") or [])
            notes.append(
                {
                    "stage": "rhea genomic analysis",
                    "what": "RHEA large-scale MUSCLE conservation tools are not available",
                    "why": reason or failure_cause or "the RHEA call failed",
                    "action": (
                        "the rhea-server auto-provisions (the orchestrator auto-builds + runs its "
                        "container from your rhea source, and the tool catalog auto-seeds on the "
                        "first apecx-mcp startup — ensure Docker is running and the rhea source is "
                        "present). The rest of the analysis still completed and is valid"
                    ),
                    "severity": "warning",
                }
            )
            bundle["proceed_notes"] = notes
        return bundle

    async def _diagnose_rhea_failure(self, exc: Exception) -> str:
        """Name the REAL cause of a RHEA-leg failure so the note points to the right fix.

        Necessary because G127 makes ``Workflow.run`` SWALLOW the inner step's exception: the
        ``exc`` the caller caught is a generic "no workflow_output" ValueError, so the underlying
        cause (the MCP server unreachable, or the tool catalog unseeded) never propagates. We
        probe the real prerequisite and report what failed. Reuses the canonical
        ``rhea_mcp_probe`` (do not roll a parallel probe). NOTE: the apecx RHEA leg is a THIN
        HTTP client (no in-process rhea import) — so there is no "rhea client not importable"
        case to diagnose."""
        mcp_url = os.environ.get("RHEA_MCP_URL", "http://localhost:3001/mcp/")
        try:
            # Import inside the try too: this runs in a NEVER-raise degrade path, so a future
            # optional import added to probes.py module-top must not reintroduce a raise here.
            from apecx_integration.infrastructure.probes import rhea_mcp_probe

            probe = await rhea_mcp_probe(mcp_url=mcp_url)
        except Exception as pe:  # noqa: BLE001 — a probe must never mask the original failure
            return (
                f"the RHEA MCP server could not be probed at {mcp_url} ({type(pe).__name__}: {pe})."
            )
        if not probe.healthy:
            return (
                f"the RHEA MCP server at {mcp_url} is unreachable or degraded "
                f"({probe.error or probe.detail}) — the orchestrator auto-builds + runs the "
                f"rhea-server container, so this is often transient (still starting up) or means "
                f"Docker/the rhea source is unavailable."
            )
        return (
            f"the RHEA client and MCP server at {mcp_url} are both reachable, but the MUSCLE run "
            f"produced no result ({type(exc).__name__}: {exc}) — the rhea TOOL CATALOG may not be "
            f"seeded yet. It auto-seeds on the first apecx-mcp startup "
            f"(InfraOrchestrator.ensure_catalog_seeded); a failure there usually means Ollama "
            f"(embedding backend) or Docker was unreachable."
        )

    @staticmethod
    def _unavailable_warning(reason: str) -> str:
        return (
            f"⚠️ RHEA genomic-analysis tools are NOT available ({reason}). The large-scale "
            "MUSCLE conservation leg did NOT run — but the rest of the end-to-end analysis "
            "(MAFFT sequence conservation, structural surface-exposure, literature) completed "
            "and remains valid. To enable the RHEA leg: the rhea-server auto-provisions as a "
            "Docker container (the orchestrator auto-builds it from your rhea source and the "
            "tool catalog auto-seeds on the first apecx-mcp startup — ensure Docker is running "
            "and the rhea source is present). The diagnosis above says which piece is missing."
        )


__all__ = ["RheaGenomicAnalysisStep", "RheaGenomicAnalysisStepConfig"]
