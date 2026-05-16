"""WebSearchContextStep — inject web-search context into the drafter prompt.

The benchmark-composition counterpart to nanobrain's ``WebSearchTool``.
``WebSearchTool`` (a ``ToolBase``, ``nanobrain/library/tools/web_search.py``)
is the generic capability; this step is the *workflow node* that wires
it into a codegen pipeline.

It mirrors the ``memory_reader`` node (``SolutionMemoryStep`` in read
mode): it sits between the router and the drafter, and it **enriches
``code_spec``** — the field the drafter consumes — with retrieved
context. Where ``memory_reader`` enriches with previously-passing
solutions, this step enriches with web search results.

I/O contract
------------

Input (the router's output shape, possibly via ``memory_reader``)::

    {"code_spec": str, "task_category": str, "entry_point": str?,
     "test_hint": str?, "function_signature": str?, ...}

Output::

    {"code_spec": "<original>\\n\\nRelevant web context:\\n...",
     "websearch_hit": bool,
     "websearch_results_used": int,
     "websearch_from_cache": bool,
     "websearch_query": str,
     ...passthrough (task_category, entry_point, test_hint,
        function_signature)}

Silent-failure discipline — and why it DIFFERS from memory_reader
------------------------------------------------------------------

``SolutionMemoryStep`` degrades gracefully: a missing memory file is
an *expected state* (an empty cache), so it logs a warning and behaves
as an empty store. **This step does NOT do that for search failures.**
A web search *backend error* (rate-limit rejection, network down,
HTTP non-200, missing API key) is a genuine fault, not an expected
state. If this step swallowed that into a silent "no context"
degrade:

* the ablation MEASUREMENT would be corrupted — a sweep where half
  the searches silently failed would measure "max_power + half-broken
  web search" and we'd wrongly conclude web search is null;
* in production, an operator would never learn their search backend
  was down.

So a backend failure **propagates LOUD** — the ``WebSearchTool``'s
``ComponentConfigurationError`` is re-raised. The benchmark runner
catches per-problem exceptions and records them as a visible
``codegen_*`` error class; nothing is hidden.

The ONE non-failure: a search that *succeeds but finds nothing*
returns zero results. That is an honest outcome — the step emits
``websearch_hit=False`` and passes ``code_spec`` through unenriched.
Exception = the search failed; empty results = the search ran and
found nothing. These are distinct.

Non-determinism
---------------

Live web results drift, so this is a **non-deterministic step** — it
is NOT under the framework's deterministic-step contract. When the
underlying ``WebSearchTool`` is configured with a ``cache_dir``, a
re-run against a populated cache IS reproducible (the cache, not the
live web, answers); the first population is still live. For
reproducible ablation sweeps, always configure the tool's cache.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

# Fields the step passes straight through to the drafter (so the
# drafter sees the same problem context it would without this node).
_PASSTHROUGH_FIELDS = (
    "task_category",
    "entry_point",
    "test_hint",
    "function_signature",
)


class WebSearchContextStepConfig(StepConfig):
    """Configuration for ``WebSearchContextStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    web_search_tool_config: str = Field(
        ...,
        description=(
            "Path to the WebSearchTool YAML config. Resolved as: "
            "absolute path used as-is; relative path tried first "
            "against this step config's directory, then against CWD."
        ),
    )

    max_query_chars: int = Field(
        default=200,
        ge=16,
        description=(
            "Cap on the search query length. The query is derived from "
            "the problem's code_spec (first line preferred, then "
            "truncated). Keeps the query focused and within DDG's "
            "query-length tolerance."
        ),
    )

    max_results: int = Field(
        default=5,
        ge=1,
        description=(
            "Number of search results to retrieve per problem. Passed "
            "to WebSearchTool.execute (overrides the tool's own "
            "default)."
        ),
    )

    snippet_chars: int = Field(
        default=320,
        ge=40,
        description=(
            "Per-result snippet length cap in the injected context "
            "block. Bounds the prompt-size blow-up from N results."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate(self) -> WebSearchContextStepConfig:
        if not self.web_search_tool_config or not self.web_search_tool_config.strip():
            raise ValueError(
                "WebSearchContextStepConfig: web_search_tool_config is "
                "required and must be a non-empty path."
            )
        return self


class WebSearchContextStep(BaseStep):
    """Enrich the drafter's ``code_spec`` with web search context."""

    COMPONENT_TYPE: str = "web_search_context_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return WebSearchContextStepConfig

    @classmethod
    def extract_component_config(cls, config: WebSearchContextStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "web_search_tool_config": config.web_search_tool_config,
            "max_query_chars": config.max_query_chars,
            "max_results": config.max_results,
            "snippet_chars": config.snippet_chars,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: WebSearchContextStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_query_chars: int = int(component_config["max_query_chars"])
        self._max_results: int = int(component_config["max_results"])
        self._snippet_chars: int = int(component_config["snippet_chars"])

        tool_config_path = self._resolve_tool_config_path(
            component_config["web_search_tool_config"],
            component_config.get("source_path"),
        )
        # Build the WebSearchTool now (at step init), not lazily — a
        # broken tool config (unknown backend, missing API key) should
        # FAIL-FAST at workflow load, not silently at the first
        # problem mid-sweep.
        from nanobrain.library.tools.web_search import WebSearchTool  # noqa: PLC0415

        self._tool = WebSearchTool.from_config(str(tool_config_path))
        log.info(
            "WebSearchContextStep %r initialized: tool=%s max_results=%d",
            self.name,
            tool_config_path,
            self._max_results,
        )

    @staticmethod
    def _resolve_tool_config_path(raw: str, source_path: str | None) -> Path:
        """Resolve the tool config path.

        Absolute -> as-is. Relative -> first against this step config's
        directory (via ``source_path``), then against CWD. FAIL-FAST
        if none of the candidates exist — a missing tool config is a
        load-time error, never a silent skip.
        """
        p = Path(raw).expanduser()
        if p.is_absolute():
            if not p.is_file():
                raise FileNotFoundError(
                    f"WebSearchContextStep: web_search_tool_config {p} (absolute) does not exist."
                )
            return p

        candidates: list[Path] = []
        if source_path:
            candidates.append(Path(source_path).resolve().parent / p)
        candidates.append((Path.cwd() / p).resolve())
        for cand in candidates:
            if cand.is_file():
                return cand
        raise FileNotFoundError(
            f"WebSearchContextStep: web_search_tool_config {raw!r} not "
            f"found. Tried: {[str(c) for c in candidates]}"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(f"WebSearchContextStep {self.name!r}: input_data must be a dict")

        # Trigger-envelope unwrap: a single-key dict whose key is not a
        # known problem field and whose value is a dict is the
        # framework's {<input_du>: payload} envelope.
        if (
            len(input_data) == 1
            and "code_spec" not in input_data
            and "problem_prompt" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        spec = input_data.get("code_spec") or input_data.get("problem_prompt")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(f"WebSearchContextStep {self.name!r}: empty code_spec/problem_prompt")

        query = self._derive_query(spec)

        # FAIL LOUD on a search backend error — see the module docstring.
        # We deliberately do NOT wrap this in a try/except that degrades
        # to "no context": a backend fault must be visible.
        result = await self._tool.execute({"query": query, "max_results": self._max_results})
        results = result.get("results") or []

        passthrough = {k: input_data.get(k) for k in _PASSTHROUGH_FIELDS}

        if not results:
            # Honest non-failure: search ran, found nothing. Pass the
            # spec through unenriched.
            log.info(
                "WebSearchContextStep %r: 0 results for query %r — passing "
                "code_spec through unenriched",
                self.name,
                query,
            )
            return {
                "code_spec": spec,
                "websearch_hit": False,
                "websearch_results_used": 0,
                "websearch_from_cache": bool(result.get("from_cache")),
                "websearch_query": query,
                **passthrough,
            }

        context_block = self._format_context(results)
        enriched = (
            f"{spec.strip()}\n\nRelevant web context (retrieved for this "
            f"problem — may or may not be applicable; use judgement):\n\n"
            f"{context_block}\n"
        )
        log.info(
            "WebSearchContextStep %r: %d results for query %r (from_cache=%s)",
            self.name,
            len(results),
            query,
            result.get("from_cache"),
        )
        return {
            "code_spec": enriched,
            "websearch_hit": True,
            "websearch_results_used": len(results),
            "websearch_from_cache": bool(result.get("from_cache")),
            "websearch_query": query,
            **passthrough,
        }

    def _derive_query(self, spec: str) -> str:
        """Turn a problem spec into a search query.

        Deterministic given the input (the non-determinism is purely
        in the web results, not in query derivation): take the first
        non-empty line of the spec, collapse whitespace, truncate to
        ``max_query_chars``.
        """
        first_line = ""
        for line in spec.splitlines():
            if line.strip():
                first_line = line.strip()
                break
        if not first_line:
            first_line = spec.strip()
        collapsed = " ".join(first_line.split())
        return collapsed[: self._max_query_chars]

    def _format_context(self, results: list[dict[str, str]]) -> str:
        """Format search results into a compact, LLM-readable block."""
        lines = []
        for i, r in enumerate(results, 1):
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", "")).strip()
            snippet = str(r.get("snippet", "")).strip()[: self._snippet_chars]
            lines.append(f"[{i}] {title}\n    {snippet}\n    ({url})")
        return "\n".join(lines)


__all__ = ["WebSearchContextStep", "WebSearchContextStepConfig"]
