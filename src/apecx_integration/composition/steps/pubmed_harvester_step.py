"""Nanobrain ``BaseStep`` wrapper around the apecx-harvesters PubMed
search + retrieve loaders.

The harvester APIs are async (``apecx_harvesters.loaders.pubmed.search``
yields PMIDs from eSearch; ``PubMedHarvester.iter_results`` fetches +
parses XML via efetch). The nanobrain executor already drives an
event loop on which our ``async process()`` runs, but the harvester
opens its OWN ``httpx.AsyncClient`` lifecycle on each call. To keep
the two loop contexts decoupled (and so a network hang here cannot
freeze the outer workflow loop), we wrap the harvester chain in
``asyncio.run`` and offload that whole synchronous-from-the-outside
block via ``asyncio.to_thread``.

Workflow shape
--------------
One of the four retrieval branches feeding the synthesis pipeline.
Output ``publications`` is a list of DataCite-shaped publication
dicts with the fields the downstream RagSynthesisStep expects::

    {doi, title, authors, year, journal, pmid}

Operator-level hooks
--------------------
- ``max_papers``: hard cap on the number of papers returned. PubMed
  searches can return 10k+ PMIDs; we stop after this many.
- ``query_template``: format string used to build the eSearch term.
  ``{query}`` substitutes the scientist query verbatim;
  ``{entities}`` substitutes a comma-joined list of entity names
  when the upstream EntityExtractionStep provided them. The default
  passes the query through unchanged.

Failure mode
------------
Network errors, eSearch errors, parse failures: logged at WARNING
and we return ``{"publications": []}``. The synthesis pipeline
tolerates an empty publications list — failing this branch must
not crash the whole workflow (the other three retrieval branches
can still produce a useful answer).

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

from apecx_integration.composition.steps import _pubmed_helpers

log = logging.getLogger(__name__)


class PubMedHarvesterStepConfig(StepConfig):
    """Step config for PubMedHarvesterStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise rather
    than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    max_papers: int = Field(
        default=5,
        description="Hard cap on the number of papers returned.",
    )
    query_template: str = Field(
        default="{query}",
        description=(
            "Format string for building the PubMed eSearch term. "
            "``{query}`` is replaced by the scientist query; "
            "``{entities}`` is replaced by a comma-joined list of "
            "entity names if upstream provided them (empty string "
            "otherwise). The default passes ``{query}`` through "
            "unchanged."
        ),
    )


class PubMedHarvesterStep(BaseStep):
    """Retrieval branch — PubMed search + fetch.

    Expected ``process()`` input::

        {"query": "alphavirus vaccine encephalitis",
         "entities": [{"name": "EEEV", "type": "pathogen"}, ...]}

    ``entities`` is optional. Each entity may be either a dict with
    a ``name`` key or a bare string.

    Return shape::

        {"publications": [
            {"doi": "10.1234/abc", "title": "...",
             "authors": ["Doe, J", ...], "year": "2024",
             "journal": "Nature", "pmid": "33594067"},
            ...
        ]}
    """

    COMPONENT_TYPE: str = "pubmed_harvester_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return PubMedHarvesterStepConfig

    @classmethod
    def extract_component_config(cls, config: PubMedHarvesterStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "max_papers": getattr(config, "max_papers", 5),
            "query_template": getattr(config, "query_template", "{query}"),
        }

    def _init_from_config(
        self,
        config: PubMedHarvesterStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_papers: int = int(component_config.get("max_papers", 5))
        self._query_template: str = str(component_config.get("query_template", "{query}"))

    # Helper logic lives in ``_pubmed_helpers`` (a stateless module)
    # so other callers — notably ``SynthesisContextAssemblyStep`` —
    # can reuse it WITHOUT instantiating a step instance via
    # ``object.__new__``. The thin wrappers below preserve the prior
    # method API for any external callers.

    @staticmethod
    def _entity_name(entity: Any) -> str | None:
        return _pubmed_helpers.entity_name(entity)

    def _build_term(self, query: str, entities: list[Any] | None) -> str:
        return _pubmed_helpers.build_term(
            query,
            entities,
            self._query_template,
            owner_name=f"PubMedHarvesterStep {self.name}",
        )

    @staticmethod
    def _container_to_dict(container: Any) -> dict[str, Any]:
        return _pubmed_helpers.container_to_dict(container)

    async def _harvest(self, term: str) -> list[dict[str, Any]]:
        return await _pubmed_helpers.harvest(term, max_papers=self._max_papers)

    def _harvest_sync(self, term: str) -> list[dict[str, Any]]:
        """Drive the async harvest on a fresh event loop. Called from a
        worker thread (see ``asyncio.to_thread`` in process())."""
        return asyncio.run(_pubmed_helpers.harvest(term, max_papers=self._max_papers))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"PubMedHarvesterStep '{self.name}': input_data must "
                f"be a dict, got {type(input_data).__name__}"
            )
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"PubMedHarvesterStep '{self.name}': input_data must "
                f"have non-empty 'query' string; got "
                f"{type(query).__name__}={query!r}"
            )

        entities = input_data.get("entities")
        if entities is not None and not isinstance(entities, list):
            raise ValueError(
                f"PubMedHarvesterStep '{self.name}': 'entities' must "
                f"be a list when present, got "
                f"{type(entities).__name__}"
            )

        term = self._build_term(query, entities)
        log.info(
            "PubMedHarvesterStep %s: term=%.120r (max_papers=%d)",
            self.name,
            term,
            self._max_papers,
        )

        self.emit_progress(f"harvesting PubMed for {term!r}")
        try:
            publications = await asyncio.to_thread(self._harvest_sync, term)
            self.emit_progress(f"PubMed: {len(publications)} publication(s) retrieved")
        except Exception as exc:
            # Network blip, eSearch error, parse glitch — log and
            # degrade to empty. The synthesis pipeline does not
            # require this branch to succeed.
            log.warning(
                "PubMedHarvesterStep %s: harvest failed (%s: %s); returning empty publications",
                self.name,
                type(exc).__name__,
                exc,
            )
            return {"publications": []}

        log.info(
            "PubMedHarvesterStep %s: returned %d publications",
            self.name,
            len(publications),
        )
        return {"publications": publications}
