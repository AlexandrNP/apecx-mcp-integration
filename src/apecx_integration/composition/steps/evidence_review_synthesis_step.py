"""EvidenceReviewSynthesisStep — LLM evidence synthesis + a DETERMINISTIC
structural-evidence section.

Terminal-but-one step of ``viral_epitope_evidence_review``. It reuses the
``apecx_integration.agents.rag_synthesis.synthesize_response`` FUNCTION (one LLM
round-trip, grounded inline citations) to turn the assembled multi-source bundle
into evidence Markdown — then appends a **deterministically rendered** structural
section.

Why the structural section is deterministic and not left to the LLM: a no-hit
must be LOUD. If we relied on the synthesizer to "mention" PDB/EMDB, a no-hit
would silently become an omission (green test, missing product signal). Instead
``render_structural_section`` always emits a section — either the structural
records found, or the explicit ``structural_note`` limitation produced upstream
by ``StructuralEvidenceStep``. The presence of the section is guaranteed; only
its content varies.

Input contract (the bundle emitted by ``StructuralEvidenceStep``)::

    {"query": str, "rag_chunks": [...], "bvbrc_genomes": [...],
     "violin_mappings": [...], "publications": [...], "globus_results": [...],
     "structural_records": [...], "structural_note": str | None}

Output::  {"markdown": "<evidence markdown + structural section>"}

The ``markdown`` key feeds a downstream ``EnvelopeStep`` (default
``markdown_input_key``), which wraps it into the terminal ``WorkflowResult``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents.globus_search._datacite import datacite_title

log = logging.getLogger(__name__)

_INPUT_KEY = "review_input"
_STRUCTURAL_HEADING = "## Structural evidence (PDB / EMDB)"


def render_evidence_fallback(
    query: str, publications: list[dict[str, Any]] | None, reason: str
) -> str:
    """Deterministic evidence summary used when LLM narrative synthesis FAILS its gate.

    Reliability contract: the evidence review must NEVER discard retrieved evidence
    because of an LLM-output-quality failure (e.g. the synthesizer's strict
    citation-grounding gate rejecting a backtick-wrapped-but-real ID). When
    ``synthesize_response`` raises, we still return the retrieved publications with
    their citations, and we NAME the reason narrative synthesis was withheld — loud,
    never silent, never empty-when-evidence-exists.
    """
    pubs = publications or []
    lines = [
        f"# Evidence review: {query}",
        "",
        f"> **Narrative synthesis was withheld** — {reason}. The retrieved evidence "
        f"is listed below verbatim so nothing is lost.",
        "",
    ]
    if pubs:
        lines.append("## Retrieved publications")
        for p in pubs:
            if not isinstance(p, dict):
                continue
            ident = p.get("doi") or p.get("id") or p.get("pmid") or ""
            title = p.get("title") or "(untitled)"
            cite = f"**[{ident}]** " if ident else ""
            lines.append(f"- {cite}*{title}*")
    else:
        lines.append("_No publications were retrieved for this query._")
    return "\n".join(lines)


def render_structural_section(
    structural_records: list[dict[str, Any]] | None,
    structural_note: str | None,
) -> str:
    """Render the structural-evidence Markdown section. Pure + LLM-free so the
    no-silent-failure guarantee is unit-testable without a live model.

    Contract: ALWAYS returns a non-empty section.
      - records present → a bulleted list (citation token + title + source);
      - else → the ``structural_note`` as a blockquote limitation;
      - else (defensive — upstream always sets a note on empty) → a generic
        explicit no-records line. Never an empty/absent section.
    """
    records = structural_records or []
    lines = [_STRUCTURAL_HEADING, ""]
    if records:
        for h in records:
            if not isinstance(h, dict):
                continue
            subject = h.get("subject") or "(unknown)"
            content = h.get("content") or {}
            # DataCite titles[0].title, not a flat "title" key (else "(untitled)").
            title = datacite_title(content)
            source = h.get("structural_source") or "structure"
            lines.append(f"- **[Globus {subject}]** *{title or '(untitled)'}* — {source}")
        return "\n".join(lines)
    # No records — the absence MUST be named, never silently empty.
    note = structural_note or (
        "No PDB or EMDB structural records were found for this query in the "
        "APECx structural corpus."
    )
    lines.append(f"> {note}")
    return "\n".join(lines)


class EvidenceReviewSynthesisStepConfig(StepConfig):
    """Config for EvidenceReviewSynthesisStep.

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

    synthesis_config_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a custom synthesis_config.yml. When None the "
            "bundled default is used (same contract as RagSynthesisStep)."
        ),
    )


class EvidenceReviewSynthesisStep(BaseStep):
    COMPONENT_TYPE: str = "evidence_review_synthesis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return EvidenceReviewSynthesisStepConfig

    @classmethod
    def extract_component_config(cls, config: EvidenceReviewSynthesisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "synthesis_config_path": getattr(config, "synthesis_config_path", None),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        from apecx_integration.agents.rag_synthesis import SynthesisConfig

        path = component_config.get("synthesis_config_path")
        if path is None:
            self._synthesis_config: SynthesisConfig | None = None
        else:
            p = Path(path)
            if not p.is_file():
                raise FileNotFoundError(
                    f"EvidenceReviewSynthesisStep: synthesis_config_path {p} does not "
                    f"exist or is not a file."
                )
            import yaml

            self._synthesis_config = SynthesisConfig.model_validate(
                yaml.safe_load(p.read_text(encoding="utf-8"))
            )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EvidenceReviewSynthesisStep '{self.name}': input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"EvidenceReviewSynthesisStep '{self.name}': bundle must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )

        from apecx_integration.agents.rag_synthesis import synthesize_response

        try:
            evidence_md = await asyncio.to_thread(
                synthesize_response,
                query.strip(),
                config=self._synthesis_config,
                rag_chunks=input_data.get("rag_chunks") or [],
                bvbrc_genomes=input_data.get("bvbrc_genomes") or [],
                violin_mappings=input_data.get("violin_mappings") or [],
                publications=input_data.get("publications") or [],
                globus_results=input_data.get("globus_results") or [],
            )
        except Exception as exc:
            # RELIABILITY: a narrative-synthesis failure (e.g. the strict
            # citation-grounding gate rejecting a backtick-wrapped real ID, an
            # empty-retrieval gate, or an LLM outage) must NOT discard the
            # retrieved evidence. Degrade LOUD to a deterministic summary that
            # names the reason and lists what was retrieved. Never return nothing
            # when evidence exists.
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "EvidenceReviewSynthesisStep %s: narrative synthesis failed (%s); "
                "degrading to deterministic evidence summary.",
                self.name,
                reason,
            )
            evidence_md = render_evidence_fallback(
                query.strip(), input_data.get("publications"), reason
            )

        # Deterministic structural section — guaranteed present (loud no-hit).
        structural_section = render_structural_section(
            input_data.get("structural_records"),
            input_data.get("structural_note"),
        )
        full_md = f"{evidence_md.rstrip()}\n\n{structural_section}\n"

        log.info(
            "EvidenceReviewSynthesisStep %s: evidence=%d chars, structural_records=%d",
            self.name,
            len(evidence_md),
            len(input_data.get("structural_records") or []),
        )
        return {"markdown": full_md}
