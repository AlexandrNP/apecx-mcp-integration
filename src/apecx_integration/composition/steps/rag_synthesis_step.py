"""Nanobrain ``BaseStep`` wrapper around
``apecx_integration.agents.rag_synthesis.synthesize_response``.

The synthesis function is synchronous (one LLM round-trip via
``ChatOpenAI.invoke``); this wrapper exposes it as an async
``Step.process()`` so the nanobrain executor can drive it inside a
workflow. The blocking LLM call is offloaded via ``asyncio.to_thread``
to keep the event loop free for sibling tasks.

Workflow shape
--------------
This step is intended as the FINAL step of the violin_bvbrc workflow
(or any workflow producing structured + retrieved data per source).
Upstream steps populate the four sources; this step assembles them
into a Markdown response with inline citations.

Operator-level hooks
--------------------
- ``synthesis_config_path``: optional override for the bundled
  ``synthesis_config.yml``. Operators tune ``min_response_chars``,
  ``min_distinct_citations``, ``max_*`` caps per deployment.
- All LLM behavior (model, base_url, api_key, temperature,
  max_tokens) flows through the standard ``APECX_LLM_*`` env vars
  via the canonical ``build_chat_llm`` factory. Local LLMs work
  out of the box.

Input contract
--------------
``input_data`` must be a dict with:
  - ``query`` (str, required, non-empty)
  - any of: ``rag_chunks``, ``bvbrc_genomes``, ``violin_mappings``,
    ``publications`` (each: ``list[dict]``, optional)

The four-source contract matches ``synthesize_response``'s kwargs
exactly. Missing keys default to empty lists; the synthesizer's
``fail_on_empty_retrieval`` gate fires if EVERY source is empty.

Output contract
---------------
Returns ``{"synthesis": <markdown>}``. The Markdown body is
post-validated by the synthesizer (size + grounded citations);
silent-failure shapes raise ``ValueError`` rather than emitting
garbage.

Authoring rule alignment (nanobrain-step-authoring skill)
---------------------------------------------------------
- Implements ``async def process()`` — never overrides ``execute()``.
- Owns no data units beyond what its YAML wires in (input/output
  unit ownership is the workflow's concern).
- Fail-fast on input contract violations at process() entry.
- ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared.
- Loaded via ``from_config(YAML)`` only — direct constructor is
  forbidden by the framework.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents.rag_synthesis import (
    SynthesisConfig,
    synthesize_response,
)

log = logging.getLogger(__name__)


class RagSynthesisStepConfig(StepConfig):
    """Step config for RagSynthesisStep.

    Extends ``StepConfig`` with one optional override:
    ``synthesis_config_path`` — path to a custom ``synthesis_config.yml``.
    When None, the bundled default is used.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load
    time rather than silently using defaults.
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

    synthesis_config_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a custom synthesis_config.yml. When None "
            "the bundled ``apecx_integration.agents.rag_synthesis."
            "synthesizer.DEFAULT_SYNTHESIS_CONFIG_PATH`` is used. "
            "Relative paths are resolved against the YAML file's "
            "directory at config-load time."
        ),
    )


class RagSynthesisStep(BaseStep):
    """Final step of the synthesis pipeline — assemble retrieved data
    into a Markdown response with inline citations.

    Expected ``process()`` input::

        {
            "query": "How do enveloped viruses fuse with host membranes?",
            "rag_chunks": [{"text": "...", "id": "...", "score": 0.9}, ...],
            "bvbrc_genomes": [{"genome_id": "11036.7", "name": "..."}, ...],
            "violin_mappings": [
                {"synonym_id": "VO_0000001",
                 "canonical_term": "Sindbis virus",
                 "query_term": "sindbis"}, ...
            ],
            "publications": [
                {"doi": "10.1234/abc", "title": "...", "authors": [...]},
                ...
            ],
        }

    Return shape::

        {"synthesis": "<markdown body with inline citations>"}

    The synthesizer's gates (``fail_on_empty_retrieval``,
    ``strict_input_validation``, ``min_response_chars``,
    ``min_distinct_citations``, ``validate_citations_against_inputs``)
    raise ``ValueError`` on contract violations. Step.process()
    propagates the error verbatim — failing fast surfaces the cause
    at the right architectural layer.
    """

    COMPONENT_TYPE: str = "rag_synthesis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return RagSynthesisStepConfig

    @classmethod
    def extract_component_config(cls, config: RagSynthesisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "synthesis_config_path": getattr(config, "synthesis_config_path", None),
        }

    def _init_from_config(
        self,
        config: RagSynthesisStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        path = component_config.get("synthesis_config_path")
        # Cache the loaded config so we don't re-read the YAML on
        # every process() invocation. Loaded eagerly — a malformed
        # path or YAML must surface at step-init, not at first
        # process() call (otherwise the failure shows up on the user's
        # query rather than on workflow boot).
        if path is None:
            self._synthesis_config: SynthesisConfig | None = None
        else:
            self._synthesis_config = self._load_synthesis_config(Path(path))

    @staticmethod
    def _load_synthesis_config(path: Path) -> SynthesisConfig:
        """Load a custom synthesis_config.yml. Errors surface here so
        an operator sees them at boot, not on the first user query."""
        if not path.is_file():
            raise FileNotFoundError(
                f"RagSynthesisStep: synthesis_config_path "
                f"{path} does not exist or is not a file. The path "
                f"is resolved relative to the wrapper YAML's "
                f"directory; check the path in the wrapper YAML."
            )
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        # extra='forbid' is set on SynthesisConfig (workspace rule);
        # a typo in the operator's YAML raises here with the
        # offending key named.
        return SynthesisConfig.model_validate(raw)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"RagSynthesisStep '{self.name}': input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )
        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"RagSynthesisStep '{self.name}': input_data must have "
                f"non-empty 'query' string; got "
                f"{type(query).__name__}={query!r}"
            )

        # Forward the four-source bundle. Missing keys default to
        # empty lists; the synthesizer's fail_on_empty_retrieval gate
        # fires only when EVERY source is empty.
        kwargs_for_synth = {
            "rag_chunks": input_data.get("rag_chunks") or [],
            "bvbrc_genomes": input_data.get("bvbrc_genomes") or [],
            "violin_mappings": input_data.get("violin_mappings") or [],
            "publications": input_data.get("publications") or [],
        }

        # synthesize_response is sync; offload the blocking LLM call.
        # We do NOT pass ``llm=...`` — the synthesizer builds its own
        # via ``build_chat_llm()`` which honors APECX_LLM_* env vars.
        # Tests can override by monkeypatching ``synthesize_response``
        # itself (no global LLM cache to clear).
        synthesis = await asyncio.to_thread(
            synthesize_response,
            query,
            config=self._synthesis_config,
            **kwargs_for_synth,
        )

        log.info(
            "RagSynthesisStep %s: produced %d-char synthesis "
            "(rag=%d, bvbrc=%d, violin=%d, pubs=%d)",
            self.name,
            len(synthesis),
            len(kwargs_for_synth["rag_chunks"]),
            len(kwargs_for_synth["bvbrc_genomes"]),
            len(kwargs_for_synth["violin_mappings"]),
            len(kwargs_for_synth["publications"]),
        )
        return {"synthesis": synthesis}
