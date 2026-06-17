"""Nanobrain ``BaseStep`` that assembles all four retrieval branches
into the synthesis input bundle consumed by ``RagSynthesisStep``.

This is the fan-in step of the synthesis pipeline.  It accepts a
plain query string, runs three retrieval branches concurrently
(domain RAG search, VIOLIN/BV-BRC tabular lookup, PubMed harvester),
and returns the complete ``synthesis_input`` dict that
``RagSynthesisStep.process()`` expects::

    {
        "query":          str,
        "rag_chunks":     list[dict],  # from DomainRagIndex
        "bvbrc_genomes":  list[dict],  # from alphavirus_genomes.tsv
        "violin_mappings": list[dict], # from VIOLIN CSVs
        "publications":   list[dict],  # from PubMed eSearch+efetch
    }

Design decision — why a single assembly step rather than four separate
steps linked in sequence:

  1. Query fanout: all three retrieval branches need the same ``query``
     string. Passing it through a sequential chain would require each
     step to propagate the query in its output, coupling their output
     shape to what the next step needs rather than to what the step
     produces semantically.

  2. Concurrency: the three branches are completely independent I/O;
     running them with ``asyncio.gather`` halves wall-clock latency
     relative to sequential chaining.

  3. Encapsulation: the assembly step becomes the clear seam between
     "retrieval" and "synthesis" — easy to mock in tests and easy to
     swap retrieval strategies without touching the synthesizer.

Operator-level hooks
--------------------
All YAML-level config fields map to the same knobs as the individual
retrieval steps:
  - ``k_rag``, ``index_path`` → domain RAG
  - ``max_publications``, ``query_template`` → PubMed
  - ``violin_data_dir``, ``bvbrc_cache_dir``,
    ``max_violin_mappings``, ``max_bvbrc_genomes`` → VIOLIN/BV-BRC

Authoring rule alignment (nanobrain-step-authoring skill)
---------------------------------------------------------
- Implements ``async def process()`` — never overrides ``execute()``.
- ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared.
- Loaded via ``from_config(YAML)`` only — direct constructor is
  forbidden by the framework.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class SynthesisContextAssemblyStepConfig(StepConfig):
    """Step config for SynthesisContextAssemblyStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at
    config-load time rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute set by ConfigBase.from_config after
    # construction. Declared here so extra="forbid" doesn't block setattr.
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        # nanobrain ConfigBase passes the raw YAML dict to Pydantic;
        # the top-level ``class`` key is a framework identifier, not a
        # config field. Strip it before extra="forbid" fires.
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    # --- Domain RAG ---
    index_path: str | None = Field(
        default=None,
        description=(
            "Optional path to the domain RAG FAISS index directory. "
            "When None, ``DomainRagIndex`` resolves the workspace default."
        ),
    )
    k_rag: int = Field(
        default=5,
        description="Number of nearest RAG chunks to return per query.",
    )

    # --- PubMed ---
    max_publications: int = Field(
        default=5,
        description="Hard cap on PubMed papers fetched per query.",
    )
    query_template: str = Field(
        default="{query}",
        description=(
            "eSearch term template. ``{query}`` = scientist query; "
            "``{entities}`` = comma-joined entity names (empty when "
            "not provided). Default passes the query through unchanged."
        ),
    )
    skip_pubmed: bool = Field(
        default=False,
        description=(
            "When True, skip the PubMed harvest branch entirely "
            "(useful for offline/test environments with no network)."
        ),
    )

    # --- VIOLIN / BV-BRC ---
    violin_data_dir: str | None = Field(
        default=None,
        description=(
            "Path to the VIOLIN CSVs directory. When None, falls back "
            "to APECX_DB_DATA_DIR env var, then workspace default."
        ),
    )
    bvbrc_cache_dir: str | None = Field(
        default=None,
        description=(
            "Path to the BV-BRC cache directory. When None, falls back to the workspace default."
        ),
    )
    max_violin_mappings: int = Field(
        default=10,
        description="Cap on VIOLIN mappings returned per query.",
    )
    max_bvbrc_genomes: int = Field(
        default=10,
        description="Cap on BV-BRC genomes returned per query.",
    )
    skip_violin: bool = Field(
        default=False,
        description=(
            "When True, skip the VIOLIN tabular lookup branch entirely "
            "(returns no VIOLIN mappings). Used by the harmonized epitope "
            "path, which retrieves VIOLIN data via Globus search instead of "
            "local CSVs. Default False keeps rag_e2e unaffected."
        ),
    )
    skip_bvbrc: bool = Field(
        default=False,
        description=(
            "When True, skip the BV-BRC tabular lookup branch entirely "
            "(returns no BV-BRC genomes). Used by the harmonized epitope "
            "path, which retrieves BV-BRC data via Globus search instead of "
            "local TSVs. Default False keeps rag_e2e unaffected."
        ),
    )

    # --- Globus Search (harvested corpus) ---
    max_globus_hits: int = Field(
        default=10,
        description=(
            "Hard cap on hits returned from the APECx Globus Search "
            "index. The index covers harvested PubMed/PDB/DataCite "
            "records. Set to 0 to skip this branch entirely."
        ),
    )
    skip_globus: bool = Field(
        default=False,
        description=(
            "When True, skip the Globus Search branch entirely "
            "(offline/sandboxed environments). The "
            "``APECX_GLOBUS_SEARCH_DISABLED=1`` env var has the same "
            "effect at the client level."
        ),
    )


class SynthesisContextAssemblyStep(BaseStep):
    """Fan-in assembly step — runs three retrieval branches
    concurrently and returns a complete synthesis input bundle.

    Expected ``process()`` input::

        {"query": "What vaccines exist for Eastern equine encephalitis?",
         "entities": [{"name": "EEEV", "type": "pathogen"}],   # optional
         "query_terms": ["EEEV", "vaccine"]}                   # optional

    ``entities`` and ``query_terms`` are optional.  When provided,
    they improve the VIOLIN/BV-BRC substring lookup.  When absent,
    the lookup falls back to simple whitespace-tokenization of the
    query (filtering stop words < 3 chars).

    Return shape (matches RagSynthesisStep input contract)::

        {
            "query":          str,
            "rag_chunks":     list[dict],
            "bvbrc_genomes":  list[dict],
            "violin_mappings": list[dict],
            "publications":   list[dict],
        }
    """

    COMPONENT_TYPE: str = "synthesis_context_assembly_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SynthesisContextAssemblyStepConfig

    @classmethod
    def extract_component_config(cls, config: SynthesisContextAssemblyStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "index_path": getattr(config, "index_path", None),
            "k_rag": getattr(config, "k_rag", 5),
            "max_publications": getattr(config, "max_publications", 5),
            "query_template": getattr(config, "query_template", "{query}"),
            "skip_pubmed": getattr(config, "skip_pubmed", False),
            "violin_data_dir": getattr(config, "violin_data_dir", None),
            "bvbrc_cache_dir": getattr(config, "bvbrc_cache_dir", None),
            "max_violin_mappings": getattr(config, "max_violin_mappings", 10),
            "max_bvbrc_genomes": getattr(config, "max_bvbrc_genomes", 10),
            "skip_violin": getattr(config, "skip_violin", False),
            "skip_bvbrc": getattr(config, "skip_bvbrc", False),
            "max_globus_hits": getattr(config, "max_globus_hits", 10),
            "skip_globus": getattr(config, "skip_globus", False),
        }

    def _init_from_config(
        self,
        config: SynthesisContextAssemblyStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        from pathlib import Path

        from apecx_integration.agents.domain_rag import DomainRagIndex

        idx_path = component_config.get("index_path")
        self._rag_index = DomainRagIndex(index_dir=Path(idx_path) if idx_path else None)
        # Boot-time existence check (one stat call; no FAISS or
        # sentence-transformer load). Per G81 (2026-05-16), the leaf
        # ``DomainRagIndex.search`` returns ``[]`` gracefully when the
        # index is missing — the RAG branch degrades to empty chunks
        # without any exception being raised. The boot-time WARNING
        # here surfaces the disabled state at workflow init (earlier
        # than first query) so operators can re-enable RAG before
        # queries arrive.
        if not self._rag_index.is_available:
            log.warning(
                "%s: domain RAG index not present at %s — "
                "RAG branch will return empty chunks for every query. "
                "RAG is DISABLED until you build the index with: "
                "`apecx-setup rag` (recommended) or `PYTHONPATH=src "
                ".venv/bin/python scripts/build_domain_rag_index.py`. "
                "The other retrieval branches (VIOLIN/BV-BRC, PubMed) "
                "continue normally; synthesis runs on those alone.",
                self.name,
                self._rag_index.index_dir,
            )
        self._k_rag: int = int(component_config.get("k_rag", 5))

        self._max_publications: int = int(component_config.get("max_publications", 5))
        self._query_template: str = str(component_config.get("query_template", "{query}"))
        self._skip_pubmed: bool = bool(component_config.get("skip_pubmed", False))

        # VIOLIN / BV-BRC lookup is delegated to stateless functions in
        # ``_violin_bvbrc_lookup`` (see ``_violin_bvbrc_lookup`` for
        # the actual code). Direct delegation avoids the prior
        # ``object.__new__(VIOLINBVBRCContextStep)`` shortcut that
        # bypassed the framework's from_config contract.
        self._violin_data_dir: str | None = component_config.get("violin_data_dir")
        self._bvbrc_cache_dir: str | None = component_config.get("bvbrc_cache_dir")
        self._max_violin: int = int(component_config.get("max_violin_mappings", 10))
        self._max_bvbrc: int = int(component_config.get("max_bvbrc_genomes", 10))
        self._skip_violin: bool = bool(component_config.get("skip_violin", False))
        self._skip_bvbrc: bool = bool(component_config.get("skip_bvbrc", False))

        self._max_globus: int = int(component_config.get("max_globus_hits", 10))
        self._skip_globus: bool = bool(component_config.get("skip_globus", False))

    # ------------------------------------------------------------------
    # Entity normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_search_terms(
        query: str,
        entities: list[Any] | None,
        query_terms: list[Any] | None,
    ) -> list[tuple[str, str]]:
        """Build (name, type) pairs for the VIOLIN/BV-BRC lookup.

        Priority: explicit entities > query_terms > naive tokenization.
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(name: str, etype: str) -> None:
            key = name.strip().lower()
            if key and key not in seen and len(key) >= 3:
                seen.add(key)
                out.append((name.strip(), etype))

        if entities:
            for ent in entities:
                if isinstance(ent, dict):
                    n = ent.get("name")
                    t = ent.get("type", "unknown")
                    if isinstance(n, str):
                        _add(n, str(t))
                elif isinstance(ent, str):
                    _add(ent, "unknown")

        if query_terms:
            for term in query_terms:
                if isinstance(term, str):
                    _add(term, "unknown")

        if not out:
            # Fallback: naively tokenize the query, skip tokens < 3 chars.
            for word in query.split():
                w = word.strip(".,;:!?()[]")
                if len(w) >= 3:
                    _add(w, "unknown")

        return out

    # ------------------------------------------------------------------
    # Retrieval helpers (sync, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _rag_search(self, query: str) -> list[dict[str, Any]]:
        return self._rag_index.search(query, k=self._k_rag)

    def _violin_bvbrc_lookup(self, terms: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
        # Short-circuit when both local tabular branches are disabled (the
        # harmonized epitope path sets both — it retrieves VIOLIN + BV-BRC data
        # via Globus search, never local CSV/TSV). No imports, no I/O.
        if self._skip_violin and self._skip_bvbrc:
            return [], []

        import os
        from pathlib import Path

        from apecx_integration.composition.steps._violin_bvbrc_lookup import (
            lookup_bvbrc,
            lookup_violin,
        )
        from apecx_integration.composition.steps.violin_bvbrc_context_step import (
            _DEFAULT_BVBRC_DIR,
            _DEFAULT_VIOLIN_DIR,
        )

        violin_dir = (
            Path(self._violin_data_dir)
            if self._violin_data_dir
            else Path(os.environ.get("APECX_DB_DATA_DIR", ""))
            if os.environ.get("APECX_DB_DATA_DIR")
            else _DEFAULT_VIOLIN_DIR
        )
        bvbrc_dir = Path(self._bvbrc_cache_dir) if self._bvbrc_cache_dir else _DEFAULT_BVBRC_DIR
        violin_mappings = (
            []
            if self._skip_violin
            else lookup_violin(
                terms,
                violin_dir,
                max_results=self._max_violin,
                owner_name=self.name,
            )
        )
        bvbrc_genomes = (
            []
            if self._skip_bvbrc
            else lookup_bvbrc(
                terms,
                bvbrc_dir,
                max_results=self._max_bvbrc,
                owner_name=self.name,
            )
        )
        return violin_mappings, bvbrc_genomes

    def _globus_search(self, query: str) -> list[dict[str, Any]]:
        """Query the APECx Globus Search index.

        Read-only access to the harvester-populated corpus (PubMed +
        PDB + DataCite records). Network call — failures are
        propagated as ``GlobusSearchUnavailableError`` and caught by
        the outer ``asyncio.gather(return_exceptions=True)``.
        """
        from apecx_integration.agents.globus_search import search

        return search(query, max_results=self._max_globus)

    def _pubmed_harvest(self, query: str, entities: list[Any] | None) -> list[dict[str, Any]]:
        """Drive the PubMed harvest synchronously on a fresh loop.

        Called from a worker thread (asyncio.to_thread) so a fresh
        event loop is the right shape — the harvester opens its own
        httpx.AsyncClient lifecycle that should not share the outer
        workflow loop.
        """
        from apecx_integration.composition.steps import _pubmed_helpers

        # Default template ("{query}") fed the whole natural-language sentence to
        # eSearch, which ANDs every token → 0 hits for verbose queries even when
        # thousands of papers exist. Anchor on the query's virus name(s) instead;
        # a custom template (with {entities}) is still honoured verbatim.
        if self._query_template == "{query}":
            term = _pubmed_helpers.build_focused_term(query, owner_name=self.name)
        else:
            term = _pubmed_helpers.build_term(
                query,
                entities,
                self._query_template,
                owner_name=self.name,
            )
        log.info(
            "%s: PubMed term=%.100r (max=%d)",
            self.name,
            term,
            self._max_publications,
        )
        try:
            return asyncio.run(_pubmed_helpers.harvest(term, max_papers=self._max_publications))
        except Exception as exc:
            log.warning(
                "%s: PubMed harvest failed (%s: %s); returning []",
                self.name,
                type(exc).__name__,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Step entry point
    # ------------------------------------------------------------------

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"SynthesisContextAssemblyStep '{self.name}': "
                f"input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap framework-wrapped input. When this step runs through
        # the trigger cascade, ``Step._execute_on_trigger`` wraps the
        # data unit value as ``{unit_name: payload}``. When called
        # directly (tests, MCP tools), the payload is passed through
        # raw. Detect both shapes by checking for the wrapper key.
        if (
            "assembly_input" in input_data
            and isinstance(input_data["assembly_input"], dict)
            and "query" not in input_data
        ):
            input_data = input_data["assembly_input"]
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"SynthesisContextAssemblyStep '{self.name}': "
                f"input_data must have non-empty 'query' string; got "
                f"{type(query).__name__}={query!r}"
            )
        query = query.strip()

        entities: list[Any] | None = input_data.get("entities")
        query_terms: list[Any] | None = input_data.get("query_terms")
        # Fail fast on wrong-shape upstream input. The internal
        # ``_extract_search_terms`` is permissive about element types
        # (skips non-dict/non-str entries silently), but if the OUTER
        # type is wrong (e.g. an upstream step packed its output as a
        # str instead of a list) we want a clear error here, not a
        # silent fall-through to whitespace tokenization.
        if entities is not None and not isinstance(entities, list):
            raise ValueError(
                f"SynthesisContextAssemblyStep '{self.name}': "
                f"'entities' must be a list or None; got "
                f"{type(entities).__name__}={entities!r}"
            )
        if query_terms is not None and not isinstance(query_terms, list):
            raise ValueError(
                f"SynthesisContextAssemblyStep '{self.name}': "
                f"'query_terms' must be a list or None; got "
                f"{type(query_terms).__name__}={query_terms!r}"
            )

        self.emit_progress("assembling context")

        terms = self._extract_search_terms(query, entities, query_terms)

        log.info(
            "%s: query=%.80r entities=%d terms=%d",
            self.name,
            query,
            len(entities) if entities else 0,
            len(terms),
        )

        # ---- Concurrent retrieval branches ----
        # ``return_exceptions=True`` so a single branch failure (e.g.
        # corrupted FAISS index, missing CSV column, network outage) is
        # logged + degraded to an empty bundle rather than crashing the
        # whole synthesis call. The synthesizer's
        # ``fail_on_empty_retrieval`` gate still fires if EVERY branch
        # ends up empty, so silent total failure is impossible.
        async def _pubmed_task() -> list[dict]:
            if self._skip_pubmed:
                return []
            return await asyncio.to_thread(self._pubmed_harvest, query, entities)

        async def _globus_task() -> list[dict]:
            # Skip when explicitly disabled OR when the cap is 0
            # (operator-level "off" without removing the field).
            if self._skip_globus or self._max_globus <= 0:
                return []
            return await asyncio.to_thread(self._globus_search, query)

        rag_result, violin_bvbrc, publications, globus_hits = await asyncio.gather(
            asyncio.to_thread(self._rag_search, query),
            asyncio.to_thread(self._violin_bvbrc_lookup, terms),
            _pubmed_task(),
            _globus_task(),
            return_exceptions=True,
        )

        if isinstance(rag_result, BaseException):
            log.warning(
                "%s: RAG branch failed (%s: %s); rag_chunks=[]",
                self.name,
                type(rag_result).__name__,
                rag_result,
            )
            rag_chunks: list[dict] = []
        else:
            rag_chunks = rag_result

        if isinstance(violin_bvbrc, BaseException):
            log.warning(
                "%s: VIOLIN/BV-BRC branch failed (%s: %s); both bundles=[]",
                self.name,
                type(violin_bvbrc).__name__,
                violin_bvbrc,
            )
            violin_mappings, bvbrc_genomes = [], []
        else:
            violin_mappings, bvbrc_genomes = violin_bvbrc

        if isinstance(publications, BaseException):
            log.warning(
                "%s: PubMed branch failed (%s: %s); publications=[]",
                self.name,
                type(publications).__name__,
                publications,
            )
            publications = []

        if isinstance(globus_hits, BaseException):
            log.warning(
                "%s: Globus Search branch failed (%s: %s); globus_results=[]",
                self.name,
                type(globus_hits).__name__,
                globus_hits,
            )
            globus_results: list[dict] = []
        else:
            globus_results = globus_hits

        log.info(
            "%s: rag=%d violin=%d bvbrc=%d pubs=%d globus=%d",
            self.name,
            len(rag_chunks),
            len(violin_mappings),
            len(bvbrc_genomes),
            len(publications),
            len(globus_results),
        )

        n_populated = sum(
            1
            for n in (
                len(rag_chunks),
                len(bvbrc_genomes),
                len(violin_mappings),
                len(publications),
                len(globus_results),
            )
            if n
        )
        self.emit_progress(f"context assembled: {n_populated} sources")

        out = {
            "query": query,
            "rag_chunks": rag_chunks,
            "bvbrc_genomes": bvbrc_genomes,
            "violin_mappings": violin_mappings,
            "publications": publications,
            "globus_results": globus_results,
        }
        # Thread the query-focus fields downstream. ``protein`` is load-bearing for the
        # structural-reasoning stage's relevance ranking (it selects the surface-antigen
        # structure that matches the requested protein, not the first by search rank);
        # ``taxon_id`` rides along for the functional-validation stage. Both originate at
        # ``normalize`` and would otherwise be dropped here (this step rebuilds the bundle).
        # ``resolution_plan`` / ``items`` / ``_map_errors`` / ``index_names`` ride through
        # when present (the viral_epitope_analysis path runs resolve → map → assemble →
        # hmerge, and hmerge — AFTER this rebuild — needs the map's per-index results +
        # the resolution plan). Absent for every other consumer (e.g. rag_e2e), so this is
        # a no-op there.
        for _focus in (
            "protein",
            "taxon_id",
            "resolved_species_name",
            "resolution_plan",
            "items",
            "_map_errors",
            "index_names",
        ):
            _val = input_data.get(_focus)
            if _val is not None:
                out[_focus] = _val
        # Stage-report scaffolding (E2-C): contribute a documented sub-report to the
        # bundle's ``stage_reports`` list. Future reasoning stages append their own;
        # the terminal synthesis renders them as a ``### Reasoning trace``.
        from apecx_integration.composition.steps._stage_report import append_stage_report

        append_stage_report(
            out,
            stage="context_assembly",
            order=1,
            markdown=(
                f"Assembled retrieval context: {len(rag_chunks)} RAG chunk(s), "
                f"{len(bvbrc_genomes)} BV-BRC genome(s), {len(violin_mappings)} VIOLIN "
                f"mapping(s), {len(publications)} publication(s), {len(globus_results)} "
                f"Globus record(s)."
            ),
            data={
                "rag_chunks": len(rag_chunks),
                "bvbrc_genomes": len(bvbrc_genomes),
                "violin_mappings": len(violin_mappings),
                "publications": len(publications),
                "globus_results": len(globus_results),
            },
        )
        return out
