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

    @staticmethod
    def _entity_name(entity: Any) -> str | None:
        """Extract a name from an entity dict or string. Returns
        ``None`` if neither shape is recognized — the caller filters."""
        if isinstance(entity, str):
            return entity.strip() or None
        if isinstance(entity, dict):
            name = entity.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _build_term(self, query: str, entities: list[Any] | None) -> str:
        """Apply ``query_template`` to build the eSearch term."""
        names: list[str] = []
        if entities:
            for ent in entities:
                name = self._entity_name(ent)
                if name:
                    names.append(name)
        entities_str = ", ".join(names)
        # Format only the keys the template references; missing keys
        # in the template are fine.
        try:
            return self._query_template.format(query=query, entities=entities_str)
        except (KeyError, IndexError) as exc:
            log.warning(
                "PubMedHarvesterStep %s: query_template format failed "
                "(%s); falling back to raw query",
                self.name,
                exc,
            )
            return query

    @staticmethod
    def _container_to_dict(container: Any) -> dict[str, Any]:
        """Project a ``PubMedContainer`` into the synthesizer's
        publication-dict shape.

        Resilient to missing fields — DataCite is permissive about
        which optional fields are populated, and PubMed XML records
        vary widely in completeness.
        """
        # title — DataCite stores a list; first entry is the primary.
        title = ""
        titles = getattr(container, "titles", None) or []
        if titles:
            title = getattr(titles[0], "title", "") or ""

        # authors — flatten Creator entries to display strings.
        # Hard cap at 25 authors. Real-world papers can have 1000+
        # consortium authors; the synthesizer's per-publication context
        # budget is small (citations + first-author display) and an
        # unbounded list bloats RAM and the LLM prompt for no benefit.
        # Truncation is signaled with an "et al." marker so the LLM
        # doesn't claim the list is exhaustive.
        _AUTHORS_CAP = 25
        authors: list[str] = []
        creators = getattr(container, "creators", None) or []
        creators_total = len(creators)
        for creator in creators[:_AUTHORS_CAP]:
            name = getattr(creator, "name", None)
            if not name:
                family = getattr(creator, "familyName", None)
                given = getattr(creator, "givenName", None)
                if family and given:
                    name = f"{family}, {given}"
                elif family:
                    name = family
                elif given:
                    name = given
            if name:
                authors.append(name)
        if creators_total > _AUTHORS_CAP:
            authors.append(f"et al. ({creators_total - _AUTHORS_CAP} more)")

        # year — DataCite has both publicationYear and a list of dates.
        year = getattr(container, "publicationYear", None) or ""
        if not year:
            for date in getattr(container, "dates", None) or []:
                date_str = getattr(date, "date", None)
                if date_str:
                    # Take the first 4 digits as the year guess.
                    year = date_str[:4]
                    break

        # journal — DataCite uses ``publisher.name``.
        publisher = getattr(container, "publisher", None)
        journal = getattr(publisher, "name", "") if publisher else ""

        # doi — the primary identifier is a DOI when present.
        doi = ""
        identifier = getattr(container, "identifier", None)
        if identifier is not None:
            doi = getattr(identifier, "identifier", "") or ""

        # pmid — stored as an alternateIdentifier with type=='PMID'.
        pmid = ""
        for alt in getattr(container, "alternateIdentifiers", None) or []:
            if getattr(alt, "alternateIdentifierType", "") == "PMID":
                pmid = getattr(alt, "alternateIdentifier", "") or ""
                if pmid:
                    break

        return {
            "doi": doi,
            "title": title,
            "authors": authors,
            "year": str(year) if year else "",
            "journal": journal,
            "pmid": pmid,
        }

    async def _harvest(self, term: str) -> list[dict[str, Any]]:
        """Run the search → fetch chain.

        Imports are local so the wrapper module loads even when the
        ``apecx_harvesters`` extra is not yet installed (the import
        error then surfaces only when this step actually runs, with
        a clear message).
        """
        from apecx_harvesters.loaders.pubmed.retrieve import (
            PubMedHarvester,
        )
        from apecx_harvesters.loaders.pubmed.search import (
            search as pubmed_search,
        )

        # Phase 1 — collect up to max_papers PMIDs.
        pmids: list[str] = []
        async for pmid in pubmed_search(term):
            pmids.append(pmid)
            if len(pmids) >= self._max_papers:
                break

        if not pmids:
            return []

        # Phase 2 — fetch + parse via the harvester. Pass the PMIDs
        # as an in-memory list (matches the iter_results contract).
        harvester = PubMedHarvester()
        publications: list[dict[str, Any]] = []
        async for result in harvester.iter_results(pmids):
            if result.ok and result.record is not None:
                publications.append(PubMedHarvesterStep._container_to_dict(result.record))
            elif result.error:
                log.warning(
                    "PubMedHarvesterStep: failed to retrieve PMID %s: %s",
                    result.id,
                    result.error,
                )
        return publications

    def _harvest_sync(self, term: str) -> list[dict[str, Any]]:
        """Drive ``_harvest`` on a fresh event loop. Called from a
        worker thread (see ``asyncio.to_thread`` in process())."""
        return asyncio.run(self._harvest(term))

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

        try:
            publications = await asyncio.to_thread(self._harvest_sync, term)
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
