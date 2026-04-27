"""Nanobrain Step wrappers around the three apecx-db-integration public
functions (workflow spec Steps 1, 3c, 5).

| Step | Class                       | Wrapped function                              | LLM calls |
|------|-----------------------------|-----------------------------------------------|-----------|
| 1    | EntityExtractionStep        | extract_entities_llm(query)                   | 1         |
| 3c   | SynonymLLMProposalsStep     | consolidated_synonym_search(query, dfs)       | 2         |
| 5    | ViolinEntityLookupStep      | enrich_matches_with_database_data(matches, dfs) | 0       |

Operator-side install requirement
---------------------------------
The ``apecx-db-integration`` package must be ``pip install -e``'d into
the same venv as ``apecx-mcp-integration``. This is intentionally NOT
declared as a ``pyproject.toml`` dep because cross-repo path deps are
not portable; see ``docs/future_work.md`` for the full rationale.

Failure mode if missing: ``ModuleNotFoundError`` at module import time
(i.e., when any wrapper YAML is loaded via ``from_config``). The error
message names the missing package; resolve with the editable install.

VIOLIN data path (Step 5)
-------------------------
``ViolinEntityLookupStep`` calls ``apecx_db_integration``'s lazy
``_get_dfs()`` accessor, which reads ``APECX_DB_DATA_DIR`` (the env
var documented by the sibling repo). The Step accepts an optional
``data_dir`` config field that overrides the env var **for the
duration of process()** — this is a deliberately narrow override
(env-var mutation is process-global; we restore the prior value in a
finally block). Steps 1 and 3c do NOT use the data path; their
wrapped functions either do not read CSVs (Step 1) or read them only
through the same lazy-cache (Step 3c, but only when the optional
``include_relevant_data=True`` is set, which we don't enable).

Mock-policy compliance
----------------------
All three wrappers route their LLM calls through the same factory
the bare functions use. The canonical factory now lives in
``apecx_integration.agents._llm_factory.build_chat_llm``; the
historical ``apecx_integration.agents.violin_bvbrc.agent.
_build_chat_llm`` name is preserved as a re-bound module attribute
so existing test monkeypatch sites keep working. Tests that need
to avoid live LLM round-trips monkeypatch that legacy attribute on
the ``violin_bvbrc.agent`` module. See
``tests/integration/test_db_integration_wrapper_steps.py`` for the
fixture shape.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

# Migrated 2026-04-27: VIOLIN agent code now lives in
# ``apecx_integration.agents.violin_bvbrc`` (cluster AR fix shipped
# in apecx-db-integration commit b54e571 + ported here verbatim
# under user directive "this repo should only depend on nanobrain
# and apecx-harvesters"). The legacy import path is no longer
# referenced from any apecx-mcp-integration production code.
from apecx_integration.agents import violin_bvbrc as apecx_db_integration  # noqa: N812
from apecx_integration.agents.violin_bvbrc import agent as _db_agent
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared config helpers
# ---------------------------------------------------------------------------

@contextmanager
def _data_dir_override(data_dir: str | None):
    """Temporarily set ``APECX_DB_DATA_DIR`` for the duration of a
    process() call. Restored in finally so the override does not leak
    to other steps in the same process.

    No-op when ``data_dir`` is ``None`` (the common case — operator
    sets the env var once at deploy time).
    """
    if data_dir is None:
        yield
        return
    prior = os.environ.get("APECX_DB_DATA_DIR")
    os.environ["APECX_DB_DATA_DIR"] = data_dir
    # Force the lazy cache to re-read on the next access. We import the
    # module attribute directly rather than mutating ``_db_agent`` to
    # keep the override block readable.
    _db_agent._DFS_CACHE = None
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("APECX_DB_DATA_DIR", None)
        else:
            os.environ["APECX_DB_DATA_DIR"] = prior
        _db_agent._DFS_CACHE = None


# ---------------------------------------------------------------------------
# Step 1 — entity extraction
# ---------------------------------------------------------------------------

class EntityExtractionStepConfig(StepConfig):
    """No extra fields. The wrapped function reads its LLM config from
    the ``APECX_LLM_*`` env vars; no Step-level override is exposed in
    first release.
    """


class EntityExtractionStep(BaseStep):
    """Workflow spec Step 1 — extract biomedical entity candidates from
    a free-text user query.

    Expected ``process()`` input::

        {"query": "find EEEV vaccines"}

    Return shape (matches Step1Output in data_unit_schemas)::

        {
            "entities": [
                {"name": "EEEV", "type": "pathogen", "confidence": 0.95},
                ...,
            ],
            "query_terms": ["EEEV", ...],
        }

    The duplicated ``query_terms`` field is so Step 3a (cache lookup)
    can read names directly without a transform-link from
    ``entities[].name``. ``entities`` stays in the output for any
    downstream consumer that needs the type / confidence metadata
    (e.g., Step 2 when wired in via the parallel-branch wrappers).
    """

    COMPONENT_TYPE: str = "entity_extraction_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return EntityExtractionStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"EntityExtractionStep '{self.name}': input_data must have "
                f"non-empty 'query' string, got {type(query).__name__}"
            )
        entities = apecx_db_integration.extract_entities_llm(query)
        query_terms = [e["name"] for e in entities if "name" in e]
        log.info(
            "EntityExtractionStep %s: extracted %d entities from query (len=%d)",
            self.name,
            len(entities),
            len(query),
        )
        return {"entities": entities, "query_terms": query_terms}


# ---------------------------------------------------------------------------
# Step 3c — LLM synonym proposals
# ---------------------------------------------------------------------------

class SynonymLLMProposalsStepConfig(StepConfig):
    data_dir: str | None = Field(
        default=None,
        description="Optional override for APECX_DB_DATA_DIR, applied only "
                    "for the duration of this step's process() call.",
    )


class SynonymLLMProposalsStep(BaseStep):
    """Workflow spec Step 3c — propose canonical synonyms for terms that
    missed the verified-synonym cache. Wraps
    ``consolidated_synonym_search`` (which itself runs entity extraction
    + LLM synonym matching, two LLM round-trips per call).

    Expected ``process()`` input::

        {"novel_terms": ["EEEV", "WEEV", "VEEV"]}

    Return shape::

        {"llm_proposals": [
            {"query_entity": "EEEV", "synonym": "EEEV stub strain", "score": 0.9},
            ...,
        ]}

    The wrapped function takes a single query string, not a list. We
    join the novel terms with a space — the function's internal
    extraction step will re-tokenize them. Suboptimal vs. a per-term
    call (one batched LLM round-trip vs. N), but matches the function's
    contract without forking it. Per-term call shape is filed in
    docs/future_work.md.
    """

    COMPONENT_TYPE: str = "synonym_llm_proposals_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SynonymLLMProposalsStepConfig

    @classmethod
    def extract_component_config(cls, config: SynonymLLMProposalsStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {**base, "data_dir": getattr(config, "data_dir", None)}

    def _init_from_config(
        self,
        config: SynonymLLMProposalsStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._data_dir: str | None = component_config.get("data_dir")

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        novel_terms = input_data.get("novel_terms")
        if not isinstance(novel_terms, list) or not all(isinstance(t, str) for t in novel_terms):
            raise ValueError(
                f"SynonymLLMProposalsStep '{self.name}': input_data must have "
                f"'novel_terms' as list[str], got {type(novel_terms).__name__}"
            )
        if not novel_terms:
            return {"llm_proposals": []}

        # The wrapped function expects a single query string. Joining is
        # the simplest contract honor; see class docstring for the
        # batching trade-off.
        synthetic_query = " ".join(novel_terms)
        with _data_dir_override(self._data_dir):
            proposals = apecx_db_integration.consolidated_synonym_search(synthetic_query)

        log.info(
            "SynonymLLMProposalsStep %s: %d proposals for %d novel terms",
            self.name,
            len(proposals),
            len(novel_terms),
        )
        return {"llm_proposals": proposals}


# ---------------------------------------------------------------------------
# Step 5 — VIOLIN entity lookup (pure pandas, no LLM)
# ---------------------------------------------------------------------------

class ViolinEntityLookupStepConfig(StepConfig):
    data_dir: str | None = Field(
        default=None,
        description="Optional override for APECX_DB_DATA_DIR, applied only "
                    "for the duration of this step's process() call.",
    )


class ViolinEntityLookupStep(BaseStep):
    """Workflow spec Step 5 — enrich resolved matches with VIOLIN +
    BV-BRC database fields. **Pure pandas, no LLM.**

    Expected ``process()`` input::

        {"matches": [
            {"query_entity": "EEEV", "synonym": "EEEV", "score": 1.0},
            ...,
        ]}

    Return shape::

        {"enriched_matches": [
            {"query_entity": "EEEV", "synonym": "EEEV", "score": 1.0,
             "relevant_data": {"vaccines": [...], "pathogens": [...]}},
            ...,
        ]}
    """

    COMPONENT_TYPE: str = "violin_entity_lookup_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return ViolinEntityLookupStepConfig

    @classmethod
    def extract_component_config(cls, config: ViolinEntityLookupStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {**base, "data_dir": getattr(config, "data_dir", None)}

    def _init_from_config(
        self,
        config: ViolinEntityLookupStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._data_dir: str | None = component_config.get("data_dir")

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        matches = input_data.get("matches")
        if not isinstance(matches, list):
            raise ValueError(
                f"ViolinEntityLookupStep '{self.name}': input_data must have "
                f"'matches' as list, got {type(matches).__name__}"
            )
        if not matches:
            return {"enriched_matches": []}

        with _data_dir_override(self._data_dir):
            dfs = _db_agent._get_dfs()
            enriched = apecx_db_integration.enrich_matches_with_database_data(matches, dfs)

        log.info(
            "ViolinEntityLookupStep %s: enriched %d matches",
            self.name,
            len(enriched),
        )
        return {"enriched_matches": enriched}
