"""Nanobrain ``BaseStep`` wrapper around
``apecx_integration.agents.domain_rag.DomainRagIndex.search``.

The DomainRagIndex.search call is synchronous (FAISS + sentence-
transformers). This wrapper exposes it as an async ``Step.process()``
so the nanobrain executor can drive it inside a workflow. The
blocking similarity search is offloaded via ``asyncio.to_thread``
to keep the event loop free for sibling tasks.

Workflow shape
--------------
This step is intended as one of the four retrieval branches of the
synthesis pipeline (alongside PubMedHarvesterStep and
VIOLINBVBRCContextStep). Output ``rag_chunks`` is consumed by the
downstream RagSynthesisStep.

Operator-level hooks
--------------------
- ``index_path``: optional path to a pre-built FAISS index directory.
  When unset, ``DomainRagIndex`` resolves a workspace default.
- ``k``: number of nearest chunks to return per query.

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

from apecx_integration.agents.domain_rag import DomainRagIndex

log = logging.getLogger(__name__)


class DomainRagSearchStepConfig(StepConfig):
    """Step config for DomainRagSearchStep.

    Extends ``StepConfig`` with two optional overrides — see field
    docstrings. ``extra='forbid'`` (workspace rule, see CLAUDE.md):
    a typo in the wrapper YAML raises here with the offending key
    named, rather than silently using a default.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    index_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a pre-built domain RAG index directory "
            "(must contain ``faiss_index.bin`` + ``metadata.json``). "
            "When None, ``DomainRagIndex`` resolves the workspace "
            "default."
        ),
    )
    k: int = Field(
        default=5,
        description="Number of nearest chunks to return per query.",
    )


class DomainRagSearchStep(BaseStep):
    """Retrieval branch — semantic search over the domain RAG index.

    Expected ``process()`` input::

        {"query": "How do enveloped viruses fuse with host membranes?"}

    Return shape::

        {"rag_chunks": [
            {"id": "...", "text": "...", "score": 0.91,
             "source": "...", "metadata": {...}},
            ...
        ]}

    Empty / blank queries raise ``ValueError`` at process() entry —
    the failure surfaces at the right architectural layer rather
    than producing an empty hit list (which would mask a wiring bug
    upstream).
    """

    COMPONENT_TYPE: str = "domain_rag_search_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return DomainRagSearchStepConfig

    @classmethod
    def extract_component_config(cls, config: DomainRagSearchStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "index_path": getattr(config, "index_path", None),
            "k": getattr(config, "k", 5),
        }

    def _init_from_config(
        self,
        config: DomainRagSearchStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._index_path: str | None = component_config.get("index_path")
        self._k: int = int(component_config.get("k", 5))
        # Cache the index instance — model + FAISS load is expensive,
        # but DomainRagIndex itself lazy-loads on first search().
        # Constructing it eagerly here lets a malformed ``index_path``
        # fail at workflow boot rather than on the first user query.
        from pathlib import Path

        self._index: DomainRagIndex = DomainRagIndex(
            index_dir=Path(self._index_path) if self._index_path else None
        )
        # Path-existence check at boot. Cheap (one stat call) — does NOT
        # trigger the sentence-transformer model download or FAISS load,
        # which both stay lazy until first search(). Logged as a
        # WARNING so workflow boot still succeeds in environments that
        # haven't built the index yet (the assembly step's
        # gather(return_exceptions=True) catches the FileNotFoundError
        # at first search and degrades to rag_chunks=[]).
        faiss_path = self._index.index_dir / "faiss_index.bin"
        if not faiss_path.is_file():
            log.warning(
                "%s: domain RAG index not found at %s; "
                "search() will raise FileNotFoundError on first call. "
                "Build the index with: PYTHONPATH=src .venv/bin/python "
                "scripts/build_domain_rag_index.py",
                self.name,
                faiss_path,
            )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"DomainRagSearchStep '{self.name}': input_data must be "
                f"a dict, got {type(input_data).__name__}"
            )
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"DomainRagSearchStep '{self.name}': input_data must "
                f"have non-empty 'query' string; got "
                f"{type(query).__name__}={query!r}"
            )

        # FAISS + sentence-transformers are blocking; offload off the
        # event loop. The first search call also pays the lazy-load
        # cost (model + index); subsequent calls only pay the encode
        # + nearest-neighbor lookup.
        chunks = await asyncio.to_thread(self._index.search, query, self._k)

        log.info(
            "DomainRagSearchStep %s: query=%.60r returned %d chunks (k=%d)",
            self.name,
            query,
            len(chunks),
            self._k,
        )
        return {"rag_chunks": chunks}
