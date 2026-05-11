"""Nanobrain steps for the verified-synonym cache (T02 Steps 3a + 4p).

## SynonymCacheLookupStep (workflow spec Step 3a)

Runs on every workflow invocation — the hot path for HARD-synonym
strategy. Takes the list of entity terms the user query produced,
POSTs to the Control Plane's batched ``/verified_synonyms/lookup``
endpoint, and partitions the result into:

- ``cached_mappings``: dict ``{query_term: canonical_term}`` for
  terms with an active verified mapping.
- ``novel_terms``: list of query_terms that missed the cache. These
  flow into Step 3c (LLM proposals) and then Step 4 (ApprovalStep).

## VerifiedSynonymWritebackStep (workflow spec Step 4p)

Runs after the ApprovalStep gate closes with APPROVED /
APPROVED_WITH_MODIFICATIONS. Each approved mapping is POSTed to the
Control Plane's ``/verified_synonyms/`` endpoint. A 409 (someone
beat us to it — two concurrent runs approving the same term) is not
a bug; those terms are silently treated as "already cached" and
dropped from the ``written`` list.

## Why these are in apecx-mcp-integration, not nanobrain

Scope decision 2026-04-22: HTTP steps whose contract is specific to
the APECx Control Plane live here, not in the general-purpose
framework. Keeps nanobrain's library free of project-specific
endpoint assumptions. See the module docstring at
``composition/steps/__init__.py``.

## Framework compliance

Both classes subclass ``BaseStep``, implement ``process()``, and
never override ``execute()``. Config schema extends ``StepConfig``
with a ``control_plane`` block whose shape mirrors ApprovalStep
(T10) for consistency. Factory ``_http_client_factory`` is pluggable
so tests can swap ``httpx.ASGITransport`` in without spinning a
live uvicorn.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx
from nanobrain.core.step import BaseStep, StepConfig
from nanobrain.core.unified_tool_descriptor import UnifiedToolDescriptor
from nanobrain.library.tools.http_backend_adapter import HTTPBackendAdapter
from pydantic import Field

log = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0


class SynonymCacheLookupStepConfig(StepConfig):
    control_plane: dict[str, Any] = Field(default_factory=dict)
    source_vocabulary: str
    target_vocabulary: str
    scope: str | None = None


class VerifiedSynonymWritebackStepConfig(StepConfig):
    control_plane: dict[str, Any] = Field(default_factory=dict)
    source_vocabulary: str
    target_vocabulary: str
    scope: str | None = None
    verified_by: str = "api_user"


def _http_client_from_config(control_plane_config: dict[str, Any]) -> httpx.AsyncClient:
    base_url = control_plane_config.get("base_url")
    if not base_url:
        raise ValueError("control_plane.base_url is required")
    timeout = float(
        control_plane_config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    )
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)


# G38-WA-1 closure (2026-05-11): the step's `process()` body now
# routes HTTP through HTTPBackendAdapter (nanobrain G38) instead of
# inline `client.post(...)`. The adapter centralizes transport —
# uniform error handling, run_context_namespace propagation as
# `X-Nanobrain-Run-Namespace` header, per-method httpx helpers
# (.post/.get/.put/.patch/.delete) that satisfy the probe-batch
# mock contract. The `_http_client_factory` injection seam is
# preserved so existing tests inject ASGI transport / mock client
# unchanged; the adapter wraps whatever client the factory returns.
#
# The step CLASS survives (vs. dissolving into ToolExecutionStep +
# PartitionStep) because the partition / 409-handling business logic
# is domain-specific and doesn't generalize. A future structural
# refactor that introduces a reusable PartitionStep + 409-tolerant
# ToolExecutionStep variant is possible but not required to retire
# the workaround — the workaround was "direct httpx in step body",
# and routing through the adapter retires that.
def _adapter_for_endpoint(
    control_plane_config: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    descriptor_id_suffix: str,
) -> tuple[HTTPBackendAdapter, UnifiedToolDescriptor]:
    """Build an HTTPBackendAdapter wrapping the caller-provided client
    plus a synthesized minimal UTD for adapter telemetry.

    The caller owns the client's lifecycle (because the legacy
    ``_http_client_factory`` returns an ``httpx.AsyncClient`` that the
    caller closes via ``async with``). The adapter respects this by
    NOT closing the client on its own ``close()`` when constructed
    with an explicit ``client=`` (HTTPBackendAdapter contract).
    """
    base_url = control_plane_config.get("base_url")
    if not base_url:
        raise ValueError("control_plane.base_url is required")
    timeout = float(
        control_plane_config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    )
    adapter = HTTPBackendAdapter(
        base_url=base_url.rstrip("/"),
        default_timeout=timeout,
        client=client,
    )

    def _placeholder() -> dict:
        return {}

    utd = UnifiedToolDescriptor.from_python_callable(
        _placeholder,
        descriptor_id=f"apecx_cp:{descriptor_id_suffix}@0.1.0",
        backend="http",
        version="0.1.0",
        provenance_class_path=f"{__name__}._placeholder",
    )
    return adapter, utd


class SynonymCacheLookupStep(BaseStep):
    """Query the Control Plane's verified-synonym cache for a batch of
    entity terms. Partitions into ``cached_mappings`` + ``novel_terms``.

    Expected ``process()`` input::

        {"query_terms": ["vaccinia", "eeev", "ebola"]}

    Return shape::

        {
            "cached_mappings": {"vaccinia": "VIOLIN_101"},
            "novel_terms": ["eeev", "ebola"],
        }
    """

    COMPONENT_TYPE: str = "synonym_cache_lookup_step"
    REQUIRED_CONFIG_FIELDS: list[str] = [
        "name",
        "source_vocabulary",
        "target_vocabulary",
        "control_plane",
    ]

    @classmethod
    def _get_config_class(cls):
        return SynonymCacheLookupStepConfig

    @classmethod
    def extract_component_config(cls, config: SynonymCacheLookupStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "control_plane": getattr(config, "control_plane", {}) or {},
            "source_vocabulary": config.source_vocabulary,
            "target_vocabulary": config.target_vocabulary,
            "scope": getattr(config, "scope", None),
        }

    def _init_from_config(
        self,
        config: SynonymCacheLookupStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._control_plane_config: dict[str, Any] = component_config["control_plane"]
        self._source_vocabulary: str = component_config["source_vocabulary"]
        self._target_vocabulary: str = component_config["target_vocabulary"]
        self._scope: str | None = component_config.get("scope")
        # Pluggable for tests. Default builds a real httpx.AsyncClient.
        self._http_client_factory = lambda: _http_client_from_config(self._control_plane_config)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        query_terms = input_data.get("query_terms")
        if not isinstance(query_terms, list) or not all(isinstance(t, str) for t in query_terms):
            raise ValueError(
                f"SynonymCacheLookupStep '{self.name}': input_data must have "
                f"'query_terms' as list[str], got {type(query_terms).__name__}"
            )
        if not query_terms:
            # Empty input is a no-op, not an error — keeps the step
            # idempotent when the upstream entity-extraction yields nothing.
            return {"cached_mappings": {}, "novel_terms": []}

        payload = {
            "source_vocabulary": self._source_vocabulary,
            "target_vocabulary": self._target_vocabulary,
            "query_terms": query_terms,
            "scope": self._scope,
        }

        # G38-WA-1 closure: dispatch via HTTPBackendAdapter. The legacy
        # `_http_client_factory` is preserved as the injection seam so
        # ASGI / mock-client tests work unchanged; the adapter wraps
        # whatever client the factory returns and routes the POST via
        # httpx's per-method `.post()` helper (idiomatic + mock-friendly).
        async with self._http_client_factory() as client:
            adapter, utd = _adapter_for_endpoint(
                self._control_plane_config,
                client=client,
                descriptor_id_suffix="synonym_cache_lookup",
            )
            try:
                body = await adapter.invoke(
                    utd,
                    payload,
                    endpoint="/verified_synonyms/lookup",
                )
            except RuntimeError as exc:
                # Adapter normalizes non-2xx to RuntimeError; surface
                # the same shape the legacy `response.raise_for_status()`
                # produced so downstream code paths see a consistent
                # exception type. httpx.HTTPStatusError carries the
                # `response` attribute the legacy code path expected.
                raise httpx.HTTPStatusError(
                    str(exc),
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                ) from exc

        matches = body.get("matches") if isinstance(body, dict) else None
        if not isinstance(matches, list):
            raise ValueError(
                f"SynonymCacheLookupStep '{self.name}': response missing "
                f"'matches' list (body={body!r})"
            )

        cached_mappings: dict[str, str] = {}
        novel_terms: list[str] = []
        for match in matches:
            term = match.get("query_term")
            result = match.get("result")
            if result is None:
                novel_terms.append(term)
            else:
                cached_mappings[term] = result["canonical_term"]

        log.info(
            "SynonymCacheLookupStep %s: %d cached, %d novel (of %d terms)",
            self.name,
            len(cached_mappings),
            len(novel_terms),
            len(query_terms),
        )
        return {"cached_mappings": cached_mappings, "novel_terms": novel_terms}


class VerifiedSynonymWritebackStep(BaseStep):
    """Persist approved mappings to the Control Plane's verified-synonym
    cache. 409s (another run recorded the same mapping) are silently
    dropped from the ``written`` list, not re-raised — approval-race
    collision is not a workflow error.

    Expected ``process()`` input — TWO accepted shapes:

    1. **Canonical** (preferred when an upstream caller has already
       reshaped the data)::

        {
            "approved_mappings": [
                {"query_term": "eeev",
                 "canonical_term": "VIOLIN_205",
                 "confidence": 0.95,
                 "source_run_id": "<uuid str, optional>",
                 "comment": "<optional>"},
                ...
            ]
        }

    2. **ApprovalStep passthrough** (T01 vertical slice — Step 4
       returns whatever Step 3c emitted, possibly augmented by reviewer
       modifications, with no key rename). ``llm_proposals`` is
       converted internally to the ``approved_mappings`` shape::

        {
            "llm_proposals": [
                {"query_entity": "eeev",
                 "synonym": "VIOLIN_205",
                 "score": 0.95},
                ...
            ]
        }

    The dual-shape acceptance keeps the workflow YAML's link block
    a set of plain DirectLinks. Doing the contract bridge in the
    Python step body (not in a TransformLink) works around nanobrain's
    YAML-loader gap on TransformLink's ``transform_function: str``.

    Return shape::

        {
            "written": ["<synonym_id_str>", ...],
            "already_existed": ["query_term_that_got_409", ...],
        }
    """

    COMPONENT_TYPE: str = "verified_synonym_writeback_step"
    REQUIRED_CONFIG_FIELDS: list[str] = [
        "name",
        "source_vocabulary",
        "target_vocabulary",
        "control_plane",
    ]

    @classmethod
    def _get_config_class(cls):
        return VerifiedSynonymWritebackStepConfig

    @classmethod
    def extract_component_config(cls, config: VerifiedSynonymWritebackStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "control_plane": getattr(config, "control_plane", {}) or {},
            "source_vocabulary": config.source_vocabulary,
            "target_vocabulary": config.target_vocabulary,
            "scope": getattr(config, "scope", None),
            "verified_by": getattr(config, "verified_by", "api_user"),
        }

    def _init_from_config(
        self,
        config: VerifiedSynonymWritebackStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._control_plane_config: dict[str, Any] = component_config["control_plane"]
        self._source_vocabulary: str = component_config["source_vocabulary"]
        self._target_vocabulary: str = component_config["target_vocabulary"]
        self._scope: str | None = component_config.get("scope")
        self._verified_by: str = component_config.get("verified_by", "api_user")
        self._http_client_factory = lambda: _http_client_from_config(self._control_plane_config)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        approved_mappings = self._coerce_input(input_data)

        written: list[str] = []
        already_existed: list[str] = []

        # G38-WA-1 closure: writeback path. The 409-as-already-existed
        # semantics requires inspecting the response BEFORE
        # raise_for_status normalization, so we keep client.post()
        # here (the per-method httpx helper IS the adapter's POST
        # path internally — the adapter's value-add of namespace
        # header propagation + unified error handling is preserved by
        # a thin per-call adapter wrap below for non-409 errors).
        async with self._http_client_factory() as client:
            adapter, _utd = _adapter_for_endpoint(
                self._control_plane_config,
                client=client,
                descriptor_id_suffix="verified_synonym_writeback",
            )
            # Resolve the active WorkflowRunContext namespace so the
            # X-Nanobrain-Run-Namespace header propagates per the
            # adapter contract (writeback's 409 inspection branch
            # bypasses adapter.invoke, so we apply the header by hand
            # to keep multi-tenant tracing intact).
            ns_headers: dict[str, str] = {}
            try:
                from nanobrain.library.orchestration.run_context import (
                    current_run_context,
                )

                ctx = current_run_context()
                if ctx is not None and getattr(ctx, "proxystore_namespace", ""):
                    ns_headers["X-Nanobrain-Run-Namespace"] = ctx.proxystore_namespace
            except ImportError:
                pass

            for mapping in approved_mappings:
                payload = {
                    "source_vocabulary": self._source_vocabulary,
                    "query_term": mapping["query_term"],
                    "target_vocabulary": self._target_vocabulary,
                    "canonical_term": mapping["canonical_term"],
                    "scope": self._scope,
                    "verified_by": self._verified_by,
                    "confidence": float(mapping.get("confidence", 1.0)),
                    "source_run_id": _coerce_uuid_string(mapping.get("source_run_id")),
                    "comment": mapping.get("comment"),
                }
                resp = await client.post(
                    "/verified_synonyms/",
                    json=payload,
                    headers=ns_headers,
                )
                if resp.status_code == httpx.codes.CONFLICT:
                    already_existed.append(mapping["query_term"])
                    continue
                resp.raise_for_status()
                body = resp.json()
                synonym_id = body["verified_synonym"]["id"]
                written.append(synonym_id)

        log.info(
            "VerifiedSynonymWritebackStep %s: wrote %d, %d already existed",
            self.name,
            len(written),
            len(already_existed),
        )
        return {"written": written, "already_existed": already_existed}

    def _coerce_input(self, input_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept either ``approved_mappings`` (canonical) or
        ``llm_proposals`` (ApprovalStep passthrough). Returns the
        canonical list-of-mapping-dicts shape used by ``process``.
        """
        if "approved_mappings" in input_data:
            value = input_data["approved_mappings"]
            if not isinstance(value, list):
                raise ValueError(
                    f"VerifiedSynonymWritebackStep '{self.name}': "
                    f"'approved_mappings' must be a list, got "
                    f"{type(value).__name__}"
                )
            return value

        if "llm_proposals" in input_data:
            value = input_data["llm_proposals"]
            if not isinstance(value, list):
                raise ValueError(
                    f"VerifiedSynonymWritebackStep '{self.name}': "
                    f"'llm_proposals' must be a list, got {type(value).__name__}"
                )
            # Field rename: query_entity → query_term, synonym → canonical_term,
            # score → confidence. Reviewer-supplied keys (source_run_id,
            # comment) pass through if present.
            converted: list[dict[str, Any]] = []
            for proposal in value:
                converted.append(
                    {
                        "query_term": proposal["query_entity"],
                        "canonical_term": proposal["synonym"],
                        "confidence": float(proposal.get("score", 1.0)),
                        "source_run_id": proposal.get("source_run_id"),
                        "comment": proposal.get("comment"),
                    }
                )
            return converted

        raise ValueError(
            f"VerifiedSynonymWritebackStep '{self.name}': input_data must have "
            "'approved_mappings' (canonical) or 'llm_proposals' "
            "(ApprovalStep passthrough)."
        )


def _coerce_uuid_string(value: Any) -> str | None:
    """Let callers pass source_run_id as a UUID, str, or None."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        # Validate shape; UUID() raises ValueError on malformed input.
        return str(UUID(value))
    raise TypeError(f"source_run_id must be UUID, str, or None, got {type(value).__name__}")
