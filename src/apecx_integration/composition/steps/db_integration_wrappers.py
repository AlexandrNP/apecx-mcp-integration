"""Nanobrain Step wrapper around the apecx-db-integration entity
extraction function (workflow spec Step 1).

| Step | Class                | Wrapped function            | LLM calls |
|------|----------------------|-----------------------------|-----------|
| 1    | EntityExtractionStep | extract_entities_llm(query) | 1         |

Operator-side install requirement
---------------------------------
The ``apecx-db-integration`` package must be ``pip install -e``'d into
the same venv as ``apecx-mcp-integration``. This is intentionally NOT
declared as a ``pyproject.toml`` dep because cross-repo path deps are
not portable; see ``docs/future_work.md`` for the full rationale.

Failure mode if missing: ``ModuleNotFoundError`` at module import time
(i.e., when any wrapper YAML is loaded via ``from_config``). The error
message names the missing package; resolve with the editable install.

Mock-policy compliance
----------------------
The wrapper routes its LLM call through the same factory the bare
function uses. The canonical factory now lives in
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
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

# Migrated 2026-04-27: entity-extraction agent code now lives in
# ``apecx_integration.agents.violin_bvbrc`` (ported under user
# directive "this repo should only depend on nanobrain and
# apecx-harvesters"). The package name is historical; it backs the
# surviving EntityExtractionStep.
from apecx_integration.agents import violin_bvbrc as apecx_db_integration  # noqa: N812

log = logging.getLogger(__name__)


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
            "query": "find EEEV vaccines",
            "entities": [
                {"name": "EEEV", "type": "pathogen", "confidence": 0.95},
                ...,
            ],
            "query_terms": ["EEEV", ...],
        }

    ``query`` is passed through so a direct link to a consumer that needs the
    original query (e.g. SynthesisContextAssemblyStep.assembly_input) is satisfied.
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
        # Pass `query` through so a downstream consumer that needs the original query
        # (e.g. SynthesisContextAssemblyStep.assembly_input, which REQUIRES it) is satisfied
        # by a direct entity_extraction -> assembly link. Without this passthrough the composer's
        # natural "extract entities then assemble" wiring fails at runtime (assembly raises on a
        # missing 'query'); the entity_extraction wrapper documents this feed relationship.
        return {"query": query, "entities": entities, "query_terms": query_terms}
