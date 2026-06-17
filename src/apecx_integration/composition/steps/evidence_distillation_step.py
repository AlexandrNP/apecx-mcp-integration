"""EvidenceDistillationStep — rank and truncate the high-volume retrieval sources
to an LLM-ready digest, WITHOUT throwing the full raw corpus at the synthesizer.

The distillation leg of ``viral_epitope_analysis``. It sits between
``FunctionalValidationStep`` and ``EvidenceReviewSynthesisStep``: the upstream
``assemble`` step now retrieves a BROAD pool per source (PubMed / Globus / BV-BRC
/ VIOLIN), and the dedicated analysis legs (structural, sequence) already distill
their own large inputs into small artifacts (PDB/EMDB records, conserved regions).
The remaining volume sources, however, flow raw into the LLM context. This step
applies a DETERMINISTIC quality ranking and keeps only the top-N per source for
the synthesizer.

Why a separate step (no-silent-failure + separation-of-concerns contract):

- Retrieval upstream is UNBOUNDED by default (it pages each source to exhaustion).
  This step is the single place that absorbs that throughput and reduces it: it
  ranks the full corpus and REPLACES each source list with its top-N. The top-N
  becomes the working set for everything downstream — the LLM context, the
  deterministic Sources ledger, the run store, and the durable artifact — so none
  of them carry thousands of raw records.
- It is not silently lossy: the pre-truncation counts are recorded in
  ``source_totals`` (and the ``distillation_note``) so coverage stays honest about
  how much was retrieved before reduction.
- The ranking is deterministic (no LLM, no randomness): score by content
  richness, recency, and query-term overlap, then a stable identity tiebreaker.
  The same bundle always distills to the same digest.

Input contract (the bundle threaded through the workflow)::

    {"query": str, "publications": list[dict], "globus_results": list[dict],
     "bvbrc_genomes": list[dict], "violin_mappings": list[dict],
     "structural_records": list[dict], ...}

Output: the same bundle with each of those source lists REPLACED by its
quality-ranked top-N, PLUS::

    {..bundle.., "source_totals": {source: retrieved_count, ...},
                 "distillation_note": str}

Authoring rule alignment (nanobrain-step-authoring skill): ``process()`` only,
``from_config`` only, ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared,
fail-fast on bad input shape, degrade-loud (never raises) so review always fires.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "distill_input"

# Tiny stoplist — the query is a short natural-language phrase; drop connective
# words so term-overlap scoring keys on the content terms (virus, protein, epitope…).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "what",
        "which",
        "how",
        "why",
        "who",
        "all",
        "any",
        "can",
        "has",
        "have",
        "into",
        "onto",
        "over",
        "its",
        "their",
        "between",
        "about",
        "more",
        "most",
    }
)


def _query_terms(query: str) -> list[str]:
    """Content tokens of the query, lowercased, len >= 3, stoplist-filtered.

    Deterministic and order-preserving (dedup keeps first occurrence)."""
    seen: dict[str, None] = {}
    for tok in re.split(r"[^a-z0-9]+", query.lower()):
        if len(tok) >= 3 and tok not in _STOPWORDS and tok not in seen:
            seen[tok] = None
    return list(seen)


def _term_overlap(text: str, terms: list[str]) -> float:
    """Fraction of query terms present in ``text``, scaled to [0, 3]."""
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t in text)
    return 3.0 * hits / len(terms)


def _blob(record: Any) -> str:
    """Shape-agnostic lowercased text of a record for term-overlap scoring.

    Robust to the DataCite-shaped Globus records (title nested under
    ``titles[0].title``) and the tabular BV-BRC/VIOLIN rows alike — we do not
    couple to any one schema, we just ask "do the query terms appear anywhere"."""
    try:
        return json.dumps(record, default=str, sort_keys=True).lower()
    except (TypeError, ValueError):
        return str(record).lower()


def _identity(record: Any) -> str:
    """Stable per-record key for a reproducible sort tiebreaker."""
    if isinstance(record, dict):
        for key in ("doi", "subject", "pmid", "genome_id", "id"):
            val = record.get(key)
            if val:
                return f"{key}:{val}"
    return _blob(record)


def _score_publication(p: dict[str, Any], terms: list[str]) -> float:
    """Deterministic quality score for a PubMed record.

    Rewards an abstract (synthesizable content), a real DOI (citable), recency,
    and query-term overlap in the title + abstract."""
    score = 0.0
    if p.get("abstract"):
        score += 2.0
    doi = p.get("doi")
    if isinstance(doi, str) and doi.startswith("10."):
        score += 2.0
    year = p.get("year")
    with contextlib.suppress(TypeError, ValueError):
        # Recency, capped: 2000 → 0.0, 2020+ → 2.0. No wall-clock dependency.
        score += min(max((int(year) - 2000) / 10.0, 0.0), 2.0)
    text = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
    score += _term_overlap(text, terms)
    return score


def _score_globus(g: dict[str, Any], terms: list[str]) -> float:
    """Deterministic quality score for a Globus/DataCite record.

    Structural records (``pdb:``/``emdb:`` subjects) get a bonus — they are the
    high-value evidence for an epitope question — plus query-term overlap."""
    score = 0.0
    subj = str(g.get("subject", "")).lower() if isinstance(g, dict) else ""
    if subj.startswith(("pdb:", "emdb:")):
        score += 1.5
    score += _term_overlap(_blob(g), terms)
    return score


def _score_generic(r: dict[str, Any], terms: list[str]) -> float:
    """Deterministic quality score for tabular rows (BV-BRC genomes, VIOLIN maps).

    Rewards query-term overlap plus record completeness (non-empty fields)."""
    score = _term_overlap(_blob(r), terms)
    if isinstance(r, dict):
        filled = sum(1 for v in r.values() if v not in (None, "", [], {}))
        score += min(filled / 10.0, 1.0)
    return score


def _rank_truncate(
    records: Any, scorer, terms: list[str], top_n: int
) -> tuple[list[dict[str, Any]], int]:
    """Return (top-N by descending score, original count). Stable + deterministic.

    Non-dict / malformed entries are dropped (they are not citable evidence)."""
    items = [r for r in (records or []) if isinstance(r, dict)]
    total = len(items)
    # Sort by (-score, identity) so ties resolve reproducibly regardless of input order.
    ranked = sorted(items, key=lambda r: (-scorer(r, terms), _identity(r)))
    return ranked[: max(top_n, 0)], total


class EvidenceDistillationStepConfig(StepConfig):
    """Config for EvidenceDistillationStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time
    rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute set by ConfigBase.from_config after construction.
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    max_publications: int = Field(
        default=15,
        description="Top-N PubMed publications kept for the LLM + Sources ledger.",
    )
    max_globus_results: int = Field(
        default=20,
        description="Top-N Globus/DataCite (incl. structural) records kept.",
    )
    max_bvbrc_genomes: int = Field(
        default=10,
        description="Top-N BV-BRC genome rows kept.",
    )
    max_violin_mappings: int = Field(
        default=10,
        description="Top-N VIOLIN mapping rows kept.",
    )
    max_structural_records: int = Field(
        default=25,
        description="Top-N PDB/EMDB structural records kept for the Structural section.",
    )


class EvidenceDistillationStep(BaseStep):
    COMPONENT_TYPE: str = "evidence_distillation_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return EvidenceDistillationStepConfig

    @classmethod
    def extract_component_config(cls, config: EvidenceDistillationStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "max_publications": getattr(config, "max_publications", 15),
            "max_globus_results": getattr(config, "max_globus_results", 20),
            "max_bvbrc_genomes": getattr(config, "max_bvbrc_genomes", 10),
            "max_violin_mappings": getattr(config, "max_violin_mappings", 10),
            "max_structural_records": getattr(config, "max_structural_records", 25),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_publications: int = int(component_config.get("max_publications", 15))
        self._max_globus: int = int(component_config.get("max_globus_results", 20))
        self._max_bvbrc: int = int(component_config.get("max_bvbrc_genomes", 10))
        self._max_violin: int = int(component_config.get("max_violin_mappings", 10))
        self._max_structural: int = int(component_config.get("max_structural_records", 25))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EvidenceDistillationStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({input_du: bundle}); direct
        # callers (tests) pass the bundle raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"EvidenceDistillationStep '{self.name}': bundle must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )
        self.emit_progress("starting evidence distillation")

        terms = _query_terms(query.strip())

        bundle = dict(input_data)  # shallow copy; we REPLACE each source list in place

        # Rank the (UNBOUNDED) retrieved corpus per source and keep only the top-N. The
        # digest's output REPLACES each source list — the top-N becomes the working set
        # that flows downstream (LLM context, deterministic Sources ledger, run store,
        # durable artifact). Carrying thousands of raw records past this point would
        # bloat every one of those; the pre-truncation totals are recorded in
        # ``source_totals`` so coverage stays honest about how much was actually pulled.
        # (sources, scorer, cap)
        specs = [
            ("publications", _score_publication, self._max_publications),
            ("globus_results", _score_globus, self._max_globus),
            ("bvbrc_genomes", _score_generic, self._max_bvbrc),
            ("violin_mappings", _score_generic, self._max_violin),
            ("structural_records", _score_globus, self._max_structural),
        ]
        self.emit_progress(f"ranking {sum(len(bundle.get(k) or []) for k, _, _ in specs)} records")
        totals: dict[str, int] = {}
        kept: dict[str, int] = {}
        for key, scorer, cap in specs:
            top, total = _rank_truncate(bundle.get(key), scorer, terms, cap)
            bundle[key] = top
            totals[key] = total
            kept[key] = len(top)

        bundle["source_totals"] = totals

        note = (
            "Ranked the retrieved corpus and kept the top-N per source (kept/retrieved): "
            + ", ".join(f"{key.split('_')[0]} {kept[key]}/{totals[key]}" for key, _, _ in specs)
            + "."
        )
        bundle["distillation_note"] = note

        from apecx_integration.composition.steps._stage_report import append_stage_report

        append_stage_report(
            bundle,
            stage="evidence_distillation",
            order=9,
            markdown=note,
            data={key: {"kept": kept[key], "total": totals[key]} for key, _, _ in specs},
        )
        log.info("EvidenceDistillationStep %s: %s", self.name, note)
        return bundle


__all__ = ["EvidenceDistillationStep", "EvidenceDistillationStepConfig"]
