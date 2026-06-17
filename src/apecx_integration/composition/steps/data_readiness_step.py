"""DataReadinessStep — C0 coverage summary (spec stage 0).

Sits AFTER ``assemble`` and BEFORE ``structural``. It summarizes the COVERAGE of the
assembled evidence bundle — how many records each retrieval branch returned — and
NAMES the gaps (branches that returned nothing) so the scientist sees, up front and
explicitly, which evidence sources are backing the answer and which are absent for this
query. Structured coverage is counted PER Globus DESTINATION INDEX (from
``harmonized_search_summary.per_index_kept`` — BV-BRC/VIOLIN now arrive via their Globus
indices, not local tabular keys), e.g. "no protabank record"; the RAG + PubMed branches
are always counted. A named gap is a real signal: it tells the reader the evidence basis
is narrower than the full source set, instead of letting a silent empty branch read as
"nothing exists".

Pure: it reads only the assemble-built bundle (no network, no I/O) and passes the bundle
through UNCHANGED apart from appending a ``data_readiness`` stage report (order 0) and
``bundle["data_readiness"]``. Structural records are fetched by the LATER ``structural``
step, so they are not counted here — this is the readiness of the assembly fan-in.

RELIABILITY (G127): never raises on a content/shape issue (it would strand the chain to
``structural`` and silently empty the whole run). It raises ONLY on a broken wiring
contract (non-dict input).
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_INPUT_KEY = "readiness_input"
_STAGE = "data_readiness"
_STAGE_ORDER = 0

# Always-present branches (RAG + PubMed). The structured-retrieval coverage is
# reported per-harmonized-index when ``harmonized_search_summary`` is on the bundle
# (the current viral_epitope_analysis path: BV-BRC + VIOLIN come from their Globus
# DESTINATION indices, not local tabular keys), else falls back to the legacy keys.
_BASE_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("rag_chunks", "RAG chunk", "no domain-RAG context"),
    ("publications", "publication", "no PubMed publication"),
)

# Legacy fallback: the pre-harmonized structured branches (used only when no
# ``harmonized_search_summary`` is present — e.g. a degraded run that never reached hmerge).
_LEGACY_STRUCTURED: tuple[tuple[str, str, str], ...] = (
    ("bvbrc_genomes", "BV-BRC genome", "no BV-BRC genome"),
    ("violin_mappings", "VIOLIN immunology mapping", "no VIOLIN immunology mapping"),
    ("globus_results", "Globus record", "no Globus harvested-corpus record"),
)


class DataReadinessStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class DataReadinessStep(BaseStep):
    COMPONENT_TYPE: str = "data_readiness_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return DataReadinessStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"DataReadinessStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({readiness_input: bundle}); direct callers
        # (tests) pass the bundle raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        self.emit_progress("starting data-readiness check")

        bundle = dict(input_data)  # passthrough copy; we add data_readiness + a report

        counts: dict[str, int] = {}
        coverage: list[tuple[str, int]] = []  # ordered (label, count) for rendering
        gaps: list[str] = []

        # 1) Always-present branches (RAG + PubMed).
        for key, label, missing_phrase in _BASE_SOURCES:
            val = bundle.get(key)
            n = len(val) if isinstance(val, list) else 0
            counts[key] = n
            coverage.append((label, n))
            if n == 0:
                gaps.append(missing_phrase)

        # 2) Structured coverage. The current path is the harmonized Globus search: count
        #    each DESTINATION index from harmonized_search_summary.per_index_kept (the real
        #    per-index hit counts recorded by HarmonizedBundleMergeStep). A degraded run that
        #    never reached hmerge has no summary → fall back to the legacy structured keys.
        summary = bundle.get("harmonized_search_summary")
        per_index = summary.get("per_index_kept") if isinstance(summary, dict) else None
        if isinstance(per_index, dict):
            for index in sorted(per_index):
                n = int(per_index.get(index) or 0)
                counts[index] = n
                coverage.append((index, n))
                if n == 0:
                    gaps.append(f"no {index} record")
        else:
            for key, label, missing_phrase in _LEGACY_STRUCTURED:
                val = bundle.get(key)
                n = len(val) if isinstance(val, list) else 0
                counts[key] = n
                coverage.append((label, n))
                if n == 0:
                    gaps.append(missing_phrase)

        total = sum(counts.values())
        n_sources = len(coverage)
        sources_available = sum(1 for _, n in coverage if n > 0)
        result = {
            "counts": counts,
            "gaps": gaps,
            "sources_available": sources_available,
            "n_sources": n_sources,
            "total_records": total,
        }
        bundle["data_readiness"] = result

        markdown = self._render_markdown(coverage, gaps, sources_available)
        append_stage_report(
            bundle, stage=_STAGE, order=_STAGE_ORDER, markdown=markdown, data=result
        )
        log.info(
            "DataReadinessStep %s: %d/%d sources populated (%d record(s)); gaps=%s",
            self.name,
            sources_available,
            n_sources,
            total,
            gaps or "none",
        )
        return bundle

    @staticmethod
    def _render_markdown(
        coverage: list[tuple[str, int]], gaps: list[str], sources_available: int
    ) -> str:
        coverage_str = ", ".join(f"{n} {label}(s)" for label, n in coverage)
        if not sources_available:
            return (
                f"Index coverage: NONE — every retrieval branch returned 0 records for this "
                f"query ({coverage_str}). The answer cannot be grounded in assembled evidence."
            )
        gap_clause = (
            f" Coverage gaps: {'; '.join(gaps)} for this query."
            if gaps
            else " All retrieval branches returned records."
        )
        return (
            f"Index coverage: {sources_available}/{len(coverage)} sources populated — "
            f"{coverage_str}.{gap_clause}"
        )


__all__ = ["DataReadinessStep", "DataReadinessStepConfig"]
