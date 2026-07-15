"""GlobusLiteratureSearchStep — the PubMed/journal-literature leg via Globus synonym search.

The literature-evidence leg of ``viral_epitope_analysis``. It searches the aggregate
APECx Globus Search index (``e74bf12a``) for JOURNAL PAPERS about the resolved virus and
folds them into the evidence bundle's ``publications`` so the downstream review cites them
alongside the direct-PubMed harvest.

Why free-text SYNONYM search, not an IRI filter (verified against live Globus 2026-07-15):
literature records live under journal-name publishers (e.g. ``"Journal of virology"``,
``"PLoS one"``) and carry NO taxon IRI — ``subjects.valueUri`` is empty on a paper. An
IRI-filtered query therefore returns ZERO literature. Papers are only findable by matching
the virus's TEXTUAL synonyms (the OR of every synonym phrase) as free text. This is the
crux of the step and the reason it is distinct from the structural leg (which is taxon /
publisher filtered).

It is the LITERATURE complement of ``StructuralEvidenceStep`` (which owns PDB/EMDB): this
step DROPS any structural hit (``pdb:``/``emdb:`` subject or ``RCSB PDB`` / ``Electron
Microscopy Data Bank`` publisher) so the two legs never double-count.

Degrade-loud contract (mirrors ``RheaGenomicAnalysisStep``): no usable synonyms, the Globus
search being disabled (``APECX_GLOBUS_SEARCH_DISABLED=1``), or a Globus outage each becomes a
NAMED note on the bundle (``globus_literature_note``) plus an empty ``globus_literature`` list
and a stage report — never a silent empty result, never a raise. The bundle passes through so
the rest of the analysis still completes.

Input contract (the bundle, after trigger-envelope unwrap)::

    {"query": str, "synonyms": list[str], "resolution_plan": {"synonyms": [...],
     "canonical_label": str}, "canonical_label": str, "resolved_species_name": str,
     "protein": str | None, "publications": list[dict], ...}

The top-level ``synonyms`` key is often absent/empty at runtime; the reliable synonym
source is ``resolution_plan.synonyms`` set by the resolve step. The builder unions both.

Output: the same bundle, plus::

    {..bundle.., "publications": <existing + de-duped globus papers>,
                 "globus_literature": list[dict],
                 "globus_literature_count": int,
                 "globus_literature_note": str | None}
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents.globus_search import client as globus_client
from apecx_integration.agents.globus_search._datacite import (
    datacite_identifiers,
    datacite_title,
)

log = logging.getLogger(__name__)

_INPUT_KEY = "lit_input"

# Structural publishers whose records belong to the PDB/EMDB leg (StructuralEvidenceStep),
# never to the literature leg. Verified server-side discriminators on e74bf12a.
_STRUCTURAL_PUBLISHERS: frozenset[str] = frozenset({"RCSB PDB", "Electron Microscopy Data Bank"})
_STRUCTURAL_SUBJECT_PREFIXES: tuple[str, ...] = ("pdb:", "emdb:")

# Topical terms AND-ed with the virus-synonym clause so the leg returns EPITOPE-relevant
# literature (antibody / antigen / neutralizing / vaccine studies), not generic mentions of the
# virus. The target protein (when known) is prepended at query-build time.
_TOPIC_TERMS: tuple[str, ...] = (
    "epitope",
    "antigen",
    "antibody",
    "neutralizing",
    "vaccine",
    "immunogenic",
)


def _publisher_name(content: Any) -> str:
    """Return the publisher/journal name of a Globus DataCite record ('' when absent).

    ``content['publisher']`` is either a nested ``{'name': ...}`` (the aggregate index shape)
    or a bare string on older normalized records — handle both.
    """
    if not isinstance(content, dict):
        return ""
    pub = content.get("publisher")
    if isinstance(pub, dict):
        return str(pub.get("name") or "")
    return str(pub or "")


def _year(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    y = content.get("publicationYear")
    return str(y) if y else ""


def _doi(content: Any) -> str:
    """Best DOI for a record: grouped ``relatedIdentifiers`` DOI first, else a DOI-typed
    top-level ``identifier``. '' when the paper carries none."""
    dois = datacite_identifiers(content).get("DOI")
    if dois:
        return dois[0]
    ident = content.get("identifier") if isinstance(content, dict) else None
    if isinstance(ident, dict) and ident.get("identifierType") == "DOI":
        v = ident.get("identifier")
        return str(v) if v else ""
    return ""


def _pmid(content: Any) -> str:
    pmids = datacite_identifiers(content).get("PMID")
    return pmids[0] if pmids else ""


def _is_structural_hit(hit: dict[str, Any]) -> bool:
    """True when the hit belongs to the PDB/EMDB structural leg (must be dropped here)."""
    subject = hit.get("subject")
    if isinstance(subject, str) and subject.lower().startswith(_STRUCTURAL_SUBJECT_PREFIXES):
        return True
    return _publisher_name(hit.get("content")) in _STRUCTURAL_PUBLISHERS


class GlobusLiteratureSearchStepConfig(StepConfig):
    """Config for GlobusLiteratureSearchStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time
    rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute set by ConfigBase.from_config after construction.
    # Declared so extra="forbid" doesn't block setattr.
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    max_records: int = Field(
        default=25,
        ge=1,
        description="Hard cap on literature hits fetched from the Globus synonym search.",
    )


class GlobusLiteratureSearchStep(BaseStep):
    COMPONENT_TYPE: str = "globus_literature_search_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return GlobusLiteratureSearchStepConfig

    @classmethod
    def extract_component_config(cls, config: GlobusLiteratureSearchStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {**base, "max_records": getattr(config, "max_records", 25)}

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_records: int = int(component_config.get("max_records", 25))

    @staticmethod
    def _build_synonym_query(
        synonyms: Any, canonical_label: Any, query: Any, protein: Any = None
    ) -> str:
        """Build a Globus ADVANCED (Lucene) query: (virus SYNONYMS) AND (epitope-topical terms).

        Search by TEXTUAL synonyms, never by taxon IRI — literature records carry no
        ``subjects.valueUri``, so an IRI filter finds zero papers. Each virus synonym is a
        quoted phrase, OR-ed; that clause is AND-ed with a topical clause (the target protein +
        epitope / antigen / antibody / neutralizing / vaccine) so the papers are epitope-RELEVANT,
        not generic virus mentions — the synonym clause alone matched hundreds of thousands of
        off-topic papers under Globus ranking. Runs in advanced mode (the caller sets
        ``advanced=True``) so the phrases + boolean are honored. When no synonyms / canonical
        label / query tokens are usable, returns ``""`` and the caller degrades loud."""
        terms: list[str] = []
        seen: set[str] = set()
        candidates: list[Any] = []
        if isinstance(synonyms, list):
            candidates.extend(synonyms)
        candidates.append(canonical_label)
        for t in candidates:
            if isinstance(t, str) and t.strip():
                norm = t.strip()
                key = norm.lower()
                if key not in seen:
                    seen.add(key)
                    terms.append(norm)
        if not terms and isinstance(query, str):
            for tok in query.split():
                norm = tok.strip()
                key = norm.lower()
                if norm and key not in seen:
                    seen.add(key)
                    terms.append(norm)
        if not terms:
            return ""
        virus_clause = " OR ".join(f'"{t}"' for t in terms)
        topic_terms: list[str] = []
        topic_seen: set[str] = set()
        for t in ([protein] if isinstance(protein, str) and protein.strip() else []) + list(
            _TOPIC_TERMS
        ):
            norm = t.strip()
            if norm and norm.lower() not in topic_seen:
                topic_seen.add(norm.lower())
                topic_terms.append(norm)
        topic_clause = " OR ".join(f'"{t}"' for t in topic_terms)
        return f"({virus_clause}) AND ({topic_clause})"

    def _hit_to_publication(self, hit: dict[str, Any]) -> dict[str, Any]:
        content = hit.get("content")
        return {
            "title": datacite_title(content) or "",
            "doi": _doi(content),
            "pmid": _pmid(content),
            "journal": _publisher_name(content),
            "year": _year(content),
            "abstract": "",
            "provenance": "globus_literature",
        }

    def _degrade(self, bundle: dict[str, Any], note: str, query_used: str) -> dict[str, Any]:
        """Degrade-loud terminal: name the reason, empty the leg, report, pass through."""
        from apecx_integration.composition.steps._stage_report import append_stage_report

        bundle["globus_literature"] = []
        bundle["globus_literature_count"] = 0
        bundle["globus_literature_note"] = note
        log.warning("GlobusLiteratureSearchStep %s: %s", self.name, note)
        append_stage_report(
            bundle,
            stage="globus_literature",
            order=4,
            markdown=note,
            data={"count": 0, "query_used": query_used, "note": note},
        )
        return bundle

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"GlobusLiteratureSearchStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({input_du: bundle}); direct callers pass raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        self.emit_progress("starting Globus literature (synonym) search")

        bundle = dict(input_data)  # shallow copy; we extend publications + add keys

        # Gather synonyms from every bundle source, in order (deduped case-insensitively in
        # the builder). The top-level bundle["synonyms"] is often absent/empty at runtime;
        # the reliable source is the resolve step's bundle["resolution_plan"]["synonyms"].
        plan = bundle.get("resolution_plan")
        plan = plan if isinstance(plan, dict) else {}
        synonyms: list[Any] = []
        top_syns = bundle.get("synonyms")
        if isinstance(top_syns, list):
            synonyms.extend(top_syns)
        plan_syns = plan.get("synonyms")
        if isinstance(plan_syns, list):
            synonyms.extend(plan_syns)
        canonical_label = (
            bundle.get("canonical_label")
            or plan.get("canonical_label")
            or bundle.get("resolved_species_name")
        )
        query = bundle.get("query")
        protein = bundle.get("protein")

        q = self._build_synonym_query(synonyms, canonical_label, query, protein)

        if not q:
            return self._degrade(
                bundle,
                "⚠️ Globus literature search was SKIPPED: the query carried no usable virus "
                "synonyms, canonical label, or query text to search by (literature records are "
                "found by textual synonym match, not by taxon IRI). No literature was added via "
                "Globus; the rest of the analysis (direct PubMed, structural, sequence) is valid.",
                q,
            )

        if globus_client._is_disabled():
            return self._degrade(
                bundle,
                "⚠️ Globus literature search is UNAVAILABLE (APECX_GLOBUS_SEARCH_DISABLED=1). "
                "No journal papers were added via the Globus synonym search; the rest of the "
                "analysis (direct PubMed, structural, sequence) still completed and is valid.",
                q,
            )

        self.emit_progress("querying Globus by synonyms")
        try:
            # advanced=True: the query uses quoted PHRASES + boolean AND/OR (Lucene); simple mode
            # would degrade it to a loose token bag matching the whole corpus off-topic.
            hits = globus_client.search(q, max_results=self._max_records, advanced=True)
        except Exception as exc:  # noqa: BLE001 — degrade-loud is the contract (never raise)
            return self._degrade(
                bundle,
                f"⚠️ Globus literature search FAILED ({type(exc).__name__}: {exc}). No journal "
                "papers were added via the Globus synonym search; the rest of the analysis "
                "(direct PubMed, structural, sequence) still completed and is valid.",
                q,
            )

        # Keep only LITERATURE — drop the structural leg's PDB/EMDB territory.
        literature_hits = [h for h in hits if isinstance(h, dict) and not _is_structural_hit(h)]
        globus_papers = [self._hit_to_publication(h) for h in literature_hits]

        # Fold into publications, de-duping against what's already there (dedup key per paper:
        # DOI if present, else PMID, else normalized title).
        existing = bundle.get("publications") or []
        if not isinstance(existing, list):
            existing = []
        seen_doi: set[str] = set()
        seen_pmid: set[str] = set()
        seen_title: set[str] = set()
        for p in existing:
            if not isinstance(p, dict):
                continue
            d = str(p.get("doi") or "").strip().lower()
            m = str(p.get("pmid") or "").strip()
            t = str(p.get("title") or "").strip().lower()
            if d:
                seen_doi.add(d)
            if m:
                seen_pmid.add(m)
            if t:
                seen_title.add(t)

        merged = list(existing)
        n_added = 0
        for paper in globus_papers:
            d = paper["doi"].strip().lower()
            m = paper["pmid"].strip()
            t = paper["title"].strip().lower()
            if d:
                dup = d in seen_doi
            elif m:
                dup = m in seen_pmid
            else:
                dup = bool(t) and t in seen_title
            if not dup:
                merged.append(paper)
                n_added += 1
                if d:
                    seen_doi.add(d)
                if m:
                    seen_pmid.add(m)
                if t:
                    seen_title.add(t)

        bundle["publications"] = merged
        bundle["globus_literature"] = globus_papers
        bundle["globus_literature_count"] = len(globus_papers)
        bundle["globus_literature_note"] = None

        self.emit_progress(f"Globus literature: {len(globus_papers)} record(s)")

        from apecx_integration.composition.steps._stage_report import append_stage_report

        markdown = (
            f"Found {len(globus_papers)} literature record(s) via Globus synonym search "
            f"({n_added} new after de-dup against the direct PubMed harvest)."
        )
        append_stage_report(
            bundle,
            stage="globus_literature",
            order=4,
            markdown=markdown,
            data={
                "count": len(globus_papers),
                "added": n_added,
                "query_used": q,
                "protein": protein,
                "note": None,
            },
        )
        log.info(
            "GlobusLiteratureSearchStep %s: q=%.80r hits=%d literature=%d added=%d",
            self.name,
            q,
            len(hits),
            len(globus_papers),
            n_added,
        )
        return bundle


__all__ = ["GlobusLiteratureSearchStep", "GlobusLiteratureSearchStepConfig"]
