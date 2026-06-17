"""SequenceEvidenceMergeStep — fan-in that folds sequence conservation into the evidence bundle.

The join point between the structural-evidence leg and the sequence-conservation leg of
``viral_epitope_analysis``. It receives two inputs via an ``AllDataReceivedTrigger``:

- ``structural_in`` — the evidence bundle from ``StructuralEvidenceStep`` (query + all source
  branches + structural_records + structural_note + the accumulated ``stage_reports``);
- ``sequence_in``  — the output of the ``sequence`` ``SubworkflowStep``: on the happy path the
  nested conserved-sites report dict ``{markdown, data:{kind:bundle, parts: conservation_result}}``
  (the nesting variant ends at the ``report`` step, so the structured result is right there under
  ``data.parts`` — no handle round-trip), or — on degrade — a named
  ``{"sequence_conservation_unavailable": <reason>}`` marker.

It emits ONE enriched bundle (the structural bundle + ``conserved_sites`` / ``conserved_regions``
threaded in) for the downstream ``EvidenceReviewSynthesisStep``, and ALWAYS appends a
``sequence_conservation`` stage report (order 1) — either summarizing the real conserved regions
+ count, or carrying the LOUD "sequence conservation unavailable: <reason>" note. The report
surfaces in the synthesis's ``## Analysis steps``; the structured ``conserved_*`` keys ride
along in the bundle for the later structural stage (map conserved positions onto 3D structure).

DEGRADE-LOUD: a missing/failed conservation result is NEVER a silent omission — the absence is
named in both the bundle (``sequence_conservation_note``) and the stage report, and the evidence
run completes with the rest of the evidence intact.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._stage_report import append_stage_report
from apecx_integration.composition.steps.sequence_conservation_subworkflow_step import (
    UNAVAILABLE_KEY,
)

log = logging.getLogger(__name__)

_STRUCTURAL_KEY = "structural_in"
_SEQUENCE_KEY = "sequence_in"
_STAGE = "sequence_conservation"
_STAGE_ORDER = 1


class SequenceEvidenceMergeStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class SequenceEvidenceMergeStep(BaseStep):
    """Merge the structural bundle + sequence conservation; emit the sequence stage report."""

    COMPONENT_TYPE: str = "sequence_evidence_merge_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SequenceEvidenceMergeStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"SequenceEvidenceMergeStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        structural = input_data.get(_STRUCTURAL_KEY)
        sequence = input_data.get(_SEQUENCE_KEY)
        # The structural leg degrades loud but ALWAYS produces its bundle; a missing structural
        # input is a real wiring failure, not a degrade case — fail loud.
        if not isinstance(structural, dict):
            raise ValueError(
                f"SequenceEvidenceMergeStep '{self.name}': expected fan-in input "
                f"{_STRUCTURAL_KEY!r} (the structural evidence bundle, a dict); got keys "
                f"{sorted(input_data)} (structural={type(structural).__name__})"
            )

        self.emit_progress("merging sequence + structural evidence")

        bundle = dict(structural)  # shallow copy; we add conserved_* keys + a stage report
        cons, note = self._resolve_conservation(sequence)

        if cons is not None:
            sites = cons.get("conserved_sites") or []
            regions = cons.get("conserved_regions") or []
            bundle["conserved_sites"] = sites
            bundle["conserved_regions"] = regions
            # Disclosure carry-forward: the per-strain records actually aligned, the aligned
            # FASTA, and the per-column identity table — for the "Data actually used" section,
            # the alignment-conservation visualization, and the per-clade loop. These are HEAVY
            # (per_column ~ alignment_length dicts); they ride on the in-memory bundle for the
            # render/viz steps but MUST be kept out of the durable JSON artifact (summaries only).
            bundle["sequence_used_records"] = cons.get("records")
            bundle["alignment_fasta"] = cons.get("alignment_fasta")
            bundle["per_column_conservation"] = cons.get("per_column")
            markdown = self._render_available(cons, sites, regions)
            data = {
                "available": True,
                "n_sequences": cons.get("n_sequences"),
                # Fetched-vs-used disclosure (summary counts only — not the record lists).
                "n_fetched": cons.get("n_fetched"),
                "n_used": cons.get("n_sequences"),
                "n_dropped_length_outlier": cons.get("n_dropped_length_outlier"),
                "n_conserved_columns": cons.get("n_conserved_columns", len(sites)),
                "n_conserved_regions": len(regions),
                "conservation_threshold": cons.get("conservation_threshold"),
                # E3-8 provenance: the aligner identity + exact version that produced the MSA.
                "aligner": cons.get("aligner"),
                "aligner_version": cons.get("aligner_version"),
            }
        else:
            # LOUD degrade — the absence is named in the bundle AND the stage report.
            bundle["conserved_sites"] = []
            bundle["conserved_regions"] = []
            bundle["sequence_conservation_note"] = note
            markdown = f"Sequence conservation unavailable: {note}"
            data = {"available": False, "note": note}

        self.emit_progress(
            f"merged: {len(bundle.get('conserved_regions') or [])} conserved regions"
        )
        append_stage_report(bundle, stage=_STAGE, order=_STAGE_ORDER, markdown=markdown, data=data)
        log.info(
            "SequenceEvidenceMergeStep %s: conservation=%s, %d conserved region(s) threaded",
            self.name,
            "available" if cons is not None else "unavailable",
            len(bundle.get("conserved_regions") or []),
        )
        return bundle

    def _resolve_conservation(self, sequence: Any) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve the conserved-sites result from the sequence stage output.

        Returns ``(conservation_result, None)`` on success or ``(None, reason)`` on a named
        degrade. The nesting variant of the conserved-sites workflow ends at its ``report`` step,
        so the structured result is carried directly under ``data.parts`` (a ``Bundle`` shape) —
        no handle round-trip.
        """
        if not isinstance(sequence, dict):
            return None, f"sequence stage produced no result ({type(sequence).__name__})"
        if UNAVAILABLE_KEY in sequence:
            return None, str(sequence[UNAVAILABLE_KEY])

        data = sequence.get("data")
        if not isinstance(data, dict):
            return None, "sequence stage returned no conservation data payload"
        parts = data.get("parts")
        if not isinstance(parts, dict) or "conserved_regions" not in parts:
            return None, "sequence stage data did not carry a conserved-sites bundle"
        return parts, None

    @staticmethod
    def _render_available(
        cons: dict[str, Any], sites: list[dict[str, Any]], regions: list[dict[str, Any]]
    ) -> str:
        n_seq = cons.get("n_sequences")
        threshold = float(cons.get("conservation_threshold", 0.9) or 0.9)
        n_cols = cons.get("n_conserved_columns", len(sites))
        top_desc = ""
        if regions:
            top = max(regions, key=lambda r: r.get("length", 0))
            motif = str(top.get("consensus", ""))
            shown = motif if len(motif) <= 30 else motif[:27] + "..."
            top_desc = (
                f" Longest conserved region: alignment cols {top.get('start')}–{top.get('end')} "
                f"(len {top.get('length')}, `{shown}`, mean identity "
                f"{top.get('mean_identity', 0.0)})."
            )
        return (
            f"Aligned {n_seq} per-strain sequences; {n_cols} conserved column(s) across "
            f"{len(regions)} conserved region(s) at ≥{threshold:.0%} identity.{top_desc}"
        )


__all__ = ["SequenceEvidenceMergeStep", "SequenceEvidenceMergeStepConfig"]
