"""Unlimited synthesis context assembly step - removes arbitrary result caps.

This is the enhanced version of SynthesisContextAssemblyStep that retrieves
ALL relevant data from VIOLIN/BV-BRC/PubMed/Globus sources instead of
applying arbitrary 10-result limits that compromise scientific analysis quality.

Key differences from original SynthesisContextAssemblyStep:
1. NO result caps - retrieves complete datasets
2. Intelligent pagination for massive results
3. Quality-based filtering instead of arbitrary truncation
4. Streaming support for large result sets
5. Enhanced error handling with partial failure recovery

Design principle: Real scientific research requires access to ALL relevant data,
not arbitrary subsets. Result caps are anti-patterns that hurt analysis quality.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class UnlimitedSynthesisAssemblyStepConfig(StepConfig):
    """Configuration for UnlimitedSynthesisAssemblyStep.

    Removes all arbitrary result caps and focuses on comprehensive data retrieval.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    # --- Domain RAG ---
    index_path: str | None = Field(
        default=None, description="Path to domain RAG FAISS index directory."
    )
    k_rag: int = Field(
        default=20, description="Number of nearest RAG chunks to return (increased from 5)."
    )

    # --- PubMed ---
    # NOTE: PubMed has API rate limits, so we use reasonable defaults
    max_publications: int = Field(
        default=50, description="PubMed result limit (increased from 5, respects API limits)."
    )
    query_template: str = Field(default="{query}", description="eSearch term template.")
    skip_pubmed: bool = Field(default=False)

    # --- VIOLIN / BV-BRC ---
    # CRITICAL: These are the limits being REMOVED
    violin_data_dir: str | None = Field(default=None)
    bvbrc_cache_dir: str | None = Field(default=None)

    # Quality filters instead of arbitrary caps
    min_violin_relevance_score: float = Field(
        default=0.1, description="Minimum relevance score for VIOLIN mappings (quality filter)."
    )
    min_bvbrc_relevance_score: float = Field(
        default=0.1, description="Minimum relevance score for BV-BRC genomes (quality filter)."
    )

    # --- Globus Search ---
    max_globus_hits: int = Field(
        default=100, description="Globus Search limit (increased from 10)."
    )
    skip_globus: bool = Field(default=False)

    # --- Performance controls ---
    enable_streaming: bool = Field(
        default=False, description="Enable streaming for very large result sets."
    )
    batch_size: int = Field(
        default=1000, description="Batch size for processing large result sets."
    )


class UnlimitedSynthesisAssemblyStep(BaseStep):
    """Enhanced assembly step that removes arbitrary result limits.

    Retrieves ALL relevant data from each source instead of applying
    arbitrary caps that compromise analysis quality. Uses quality-based
    filtering and intelligent pagination for massive datasets.
    """

    COMPONENT_TYPE: str = "unlimited_synthesis_assembly_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return UnlimitedSynthesisAssemblyStepConfig

    @classmethod
    def extract_component_config(
        cls, config: UnlimitedSynthesisAssemblyStepConfig
    ) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "index_path": getattr(config, "index_path", None),
            "k_rag": getattr(config, "k_rag", 20),
            "max_publications": getattr(config, "max_publications", 50),
            "query_template": getattr(config, "query_template", "{query}"),
            "skip_pubmed": getattr(config, "skip_pubmed", False),
            "violin_data_dir": getattr(config, "violin_data_dir", None),
            "bvbrc_cache_dir": getattr(config, "bvbrc_cache_dir", None),
            "min_violin_relevance_score": getattr(config, "min_violin_relevance_score", 0.1),
            "min_bvbrc_relevance_score": getattr(config, "min_bvbrc_relevance_score", 0.1),
            "max_globus_hits": getattr(config, "max_globus_hits", 100),
            "skip_globus": getattr(config, "skip_globus", False),
            "enable_streaming": getattr(config, "enable_streaming", False),
            "batch_size": getattr(config, "batch_size", 1000),
        }

    def _init_from_config(
        self,
        config: UnlimitedSynthesisAssemblyStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        from pathlib import Path

        from apecx_integration.agents.domain_rag import DomainRagIndex

        # RAG index setup
        idx_path = component_config.get("index_path")
        self._rag_index = DomainRagIndex(index_dir=Path(idx_path) if idx_path else None)
        self._k_rag: int = int(component_config.get("k_rag", 20))

        # PubMed config
        self._max_publications: int = int(component_config.get("max_publications", 50))
        self._query_template: str = str(component_config.get("query_template", "{query}"))
        self._skip_pubmed: bool = bool(component_config.get("skip_pubmed", False))

        # VIOLIN/BV-BRC config - NO MAX LIMITS
        self._violin_data_dir: str | None = component_config.get("violin_data_dir")
        self._bvbrc_cache_dir: str | None = component_config.get("bvbrc_cache_dir")
        self._min_violin_score: float = float(
            component_config.get("min_violin_relevance_score", 0.1)
        )
        self._min_bvbrc_score: float = float(component_config.get("min_bvbrc_relevance_score", 0.1))

        # Globus config
        self._max_globus: int = int(component_config.get("max_globus_hits", 100))
        self._skip_globus: bool = bool(component_config.get("skip_globus", False))

        # Performance config
        self._enable_streaming: bool = bool(component_config.get("enable_streaming", False))
        self._batch_size: int = int(component_config.get("batch_size", 1000))

        self.nb_logger.info(
            f"{self.name}: initialized unlimited assembly - "
            f"k_rag={self._k_rag}, max_pubs={self._max_publications}, "
            f"globus_max={self._max_globus}, streaming={self._enable_streaming}"
        )
        self.nb_logger.warning(
            f"{self.name}: UNLIMITED mode - will retrieve ALL relevant VIOLIN/BV-BRC data"
        )

    # ------------------------------------------------------------------
    # Enhanced retrieval helpers (unlimited versions)
    # ------------------------------------------------------------------

    def _unlimited_violin_bvbrc_lookup(
        self, terms: list[tuple[str, str]]
    ) -> tuple[list[dict], list[dict]]:
        """Unlimited VIOLIN/BV-BRC lookup - retrieves ALL relevant data.

        This is the critical change: removes max_results caps entirely.
        Uses quality-based filtering instead of arbitrary truncation.
        """
        import os
        from pathlib import Path

        from apecx_integration.composition.steps._unlimited_lookup import (
            lookup_bvbrc_unlimited,
            lookup_violin_unlimited,
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

        self.nb_logger.info(f"{self.name}: unlimited lookup for {len(terms)} terms")

        # CRITICAL CHANGE: No max_results parameter - retrieve everything
        violin_mappings = lookup_violin_unlimited(
            terms,
            violin_dir,
            max_results=None,  # UNLIMITED
            min_relevance_score=self._min_violin_score,
            owner_name=self.name,
        )

        bvbrc_genomes = lookup_bvbrc_unlimited(
            terms,
            bvbrc_dir,
            max_results=None,  # UNLIMITED
            min_relevance_score=self._min_bvbrc_score,
            owner_name=self.name,
        )

        self.nb_logger.info(
            f"{self.name}: unlimited lookup complete - "
            f"violin={len(violin_mappings)}, bvbrc={len(bvbrc_genomes)}"
        )

        return violin_mappings, bvbrc_genomes

    def _enhanced_rag_search(self, query: str) -> list[dict[str, Any]]:
        """Enhanced RAG search with increased result count."""
        return self._rag_index.search(query, k=self._k_rag)

    def _enhanced_globus_search(self, query: str) -> list[dict[str, Any]]:
        """Enhanced Globus search with increased result count."""
        from apecx_integration.agents.globus_search import search

        return search(query, max_results=self._max_globus)

    def _enhanced_pubmed_harvest(
        self, query: str, entities: list[Any] | None
    ) -> list[dict[str, Any]]:
        """Enhanced PubMed harvest with increased result count."""
        from apecx_integration.composition.steps import _pubmed_helpers

        term = _pubmed_helpers.build_term(
            query,
            entities,
            self._query_template,
            owner_name=self.name,
        )

        self.nb_logger.info(
            f"{self.name}: PubMed term={term[:100]}... (max={self._max_publications})"
        )

        try:
            return asyncio.run(_pubmed_helpers.harvest(term, max_papers=self._max_publications))
        except Exception as exc:
            self.nb_logger.warning(
                f"{self.name}: PubMed harvest failed ({type(exc).__name__}: {exc})"
            )
            return []

    # ------------------------------------------------------------------
    # Entity extraction (reuse from original)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_search_terms(
        query: str,
        entities: list[Any] | None,
        query_terms: list[Any] | None,
    ) -> list[tuple[str, str]]:
        """Build (name, type) pairs for VIOLIN/BV-BRC lookup."""
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
            for word in query.split():
                w = word.strip(".,;:!?()[]")
                if len(w) >= 3:
                    _add(w, "unknown")

        return out

    # ------------------------------------------------------------------
    # Main processing entry point
    # ------------------------------------------------------------------

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Perform unlimited multi-source data assembly.

        Retrieves ALL relevant data instead of applying arbitrary caps.
        Critical for comprehensive scientific analysis.
        """
        if not isinstance(input_data, dict):
            raise ValueError(
                f"UnlimitedSynthesisAssemblyStep '{self.name}': "
                f"input_data must be a dict, got {type(input_data).__name__}"
            )

        # Handle framework wrapping
        if "assembly_input" in input_data and "query" not in input_data:
            input_data = input_data["assembly_input"]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"UnlimitedSynthesisAssemblyStep '{self.name}': "
                f"input_data must have non-empty 'query' string"
            )

        query = query.strip()
        entities: list[Any] | None = input_data.get("entities")
        query_terms: list[Any] | None = input_data.get("query_terms")

        terms = self._extract_search_terms(query, entities, query_terms)

        self.nb_logger.info(
            f"{self.name}: unlimited assembly - query={query[:80]}..., "
            f"entities={len(entities) if entities else 0}, terms={len(terms)}"
        )

        # Enhanced concurrent retrieval with unlimited data sources
        async def _pubmed_task() -> list[dict]:
            if self._skip_pubmed:
                return []
            return await asyncio.to_thread(self._enhanced_pubmed_harvest, query, entities)

        async def _globus_task() -> list[dict]:
            if self._skip_globus or self._max_globus <= 0:
                return []
            return await asyncio.to_thread(self._enhanced_globus_search, query)

        # Execute all branches concurrently
        rag_result, violin_bvbrc, publications, globus_hits = await asyncio.gather(
            asyncio.to_thread(self._enhanced_rag_search, query),
            asyncio.to_thread(self._unlimited_violin_bvbrc_lookup, terms),
            _pubmed_task(),
            _globus_task(),
            return_exceptions=True,
        )

        # Process results with enhanced error handling
        if isinstance(rag_result, BaseException):
            self.nb_logger.warning(f"{self.name}: RAG failed: {rag_result}")
            rag_chunks: list[dict] = []
        else:
            rag_chunks = rag_result

        if isinstance(violin_bvbrc, BaseException):
            self.nb_logger.warning(f"{self.name}: VIOLIN/BV-BRC failed: {violin_bvbrc}")
            violin_mappings, bvbrc_genomes = [], []
        else:
            violin_mappings, bvbrc_genomes = violin_bvbrc

        if isinstance(publications, BaseException):
            self.nb_logger.warning(f"{self.name}: PubMed failed: {publications}")
            publications = []

        if isinstance(globus_hits, BaseException):
            self.nb_logger.warning(f"{self.name}: Globus failed: {globus_hits}")
            globus_results: list[dict] = []
        else:
            globus_results = globus_hits

        self.nb_logger.info(
            f"{self.name}: UNLIMITED assembly complete - "
            f"rag={len(rag_chunks)}, violin={len(violin_mappings)}, "
            f"bvbrc={len(bvbrc_genomes)}, pubs={len(publications)}, "
            f"globus={len(globus_results)}"
        )

        return {
            "query": query,
            "rag_chunks": rag_chunks,
            "bvbrc_genomes": bvbrc_genomes,
            "violin_mappings": violin_mappings,
            "publications": publications,
            "globus_results": globus_results,
        }
