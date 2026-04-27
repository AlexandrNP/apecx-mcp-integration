"""Probe batch 21 — internal step↔Control-Plane HTTP bridge contracts
(probes 530-554).

The MCP layer was found broken in batch 18 (clusters AO + AP) by
field-name mismatches between the MCP tool wrapper and its target
Pydantic schema. This batch audits the *internal* (non-MCP) HTTP
bridges that nanobrain steps use to talk to the Control Plane:

  - nanobrain.library.steps.approval_step.ApprovalStep →
    POST /approvals/  (CreateApprovalRequest)

  - apecx_integration.composition.steps.synonym_cache.
    SynonymCacheLookupStep → POST /verified_synonyms/lookup
    (VerifiedSynonymLookupRequest)

  - apecx_integration.composition.steps.synonym_cache.
    VerifiedSynonymWritebackStep → POST /verified_synonyms/
    (CreateVerifiedSynonymRequest)

A field-name drift here would manifest as Pydantic ValidationError
on the *route* side (ApprovalStep gets 422 from the Control Plane)
or — if the schema accepted more — silent data loss. Either way the
scientist sees a workflow run that hangs or fails mid-way.

All probes are pure-Python: source-text inspection, schema field
introspection, and end-to-end exercising via httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from typing import Any

import httpx
import pytest


pytestmark = pytest.mark.integration


_VALID_UUID_STR = "550e8400-e29b-41d4-a716-446655440000"


# ---------------------------------------------------------------------------
# ApprovalStep → CreateApprovalRequest bridge — probes 530-535
# ---------------------------------------------------------------------------


def _approval_step_create_payload_keys() -> set[str]:
    """Source-inspection: extract the set of keys the nanobrain
    ApprovalStep._create_approval method places in its payload dict.

    We can't easily run a real ApprovalStep without nanobrain's
    from_config setup, so we lock the contract via source AST."""
    from nanobrain.library.steps import approval_step
    src = inspect.getsource(approval_step)
    # Find: payload = {  ... } block inside _create_approval
    m = re.search(
        r"_create_approval\b.*?payload\s*=\s*\{(.*?)\}",
        src,
        re.DOTALL,
    )
    assert m, "Could not locate ApprovalStep._create_approval payload literal"
    block = m.group(1)
    keys = set(re.findall(r'"([a-z_]+)"\s*:', block))
    return keys


def test_probe_530_approval_step_payload_keys_locked() -> None:
    """The exact set of keys ApprovalStep posts to /approvals/.
    Locking it here means a future field rename in the step source
    breaks this probe before it breaks every workflow run."""
    keys = _approval_step_create_payload_keys()
    assert keys == {
        "run_id", "step_id", "kind", "summary",
        "artifact_ids", "policy",
    }, f"PROBE 530: ApprovalStep payload keys drifted: {keys}"


def test_probe_531_approval_step_keys_subset_of_schema() -> None:
    """Every key the step sends MUST be a field in
    CreateApprovalRequest. Otherwise the route's
    extra='forbid' rejects with ValidationError."""
    from apecx_integration.control_plane.schemas.api import (
        CreateApprovalRequest,
    )
    schema_fields = set(CreateApprovalRequest.model_fields.keys())
    payload_keys = _approval_step_create_payload_keys()
    extra = payload_keys - schema_fields
    assert not extra, (
        f"PROBE 531: ApprovalStep sends keys the schema rejects: {extra}"
    )


def test_probe_532_approval_step_response_path_matches_schema() -> None:
    """ApprovalStep accesses ``body['approval']['id']`` from the POST
    response. CreateApprovalResponse must have an 'approval' field
    that contains an Approval entity with an 'id' field."""
    from apecx_integration.control_plane.schemas.api import (
        CreateApprovalResponse,
    )
    from apecx_integration.control_plane.schemas.entities import Approval
    fields = CreateApprovalResponse.model_fields
    assert "approval" in fields
    approval_fields = Approval.model_fields
    assert "id" in approval_fields


def test_probe_533_approval_kind_enum_locked() -> None:
    """The ApprovalStep's _gate_kind must be one of the values in
    ApprovalKind. A new kind shipped without updating the enum
    would surface as schema ValidationError."""
    from apecx_integration.control_plane.schemas.enums import ApprovalKind
    expected = {"hard", "soft", "silent", "allocation"}
    actual = {k.value for k in ApprovalKind}
    assert actual == expected, (
        f"PROBE 533: ApprovalKind enum drifted: {actual} != {expected}"
    )


def test_probe_534_approval_step_required_kwargs_present() -> None:
    """ApprovalStep.process requires run_id and step_id kwargs.
    Missing either must fail-fast with a friendly message — not
    an obscure KeyError or implicit None somewhere downstream."""
    from nanobrain.library.steps import approval_step
    src = inspect.getsource(approval_step)
    # Must require BOTH run_id and step_id
    assert "run_id" in src and "step_id" in src
    # Must raise when either is missing
    assert (
        "requires run_id and step_id" in src
        or "run_id and step_id kwargs" in src
    )


def test_probe_535_approval_response_unwrap_path_correct() -> None:
    """The unwrap path is body['approval']['id'] — anything else
    would mean the step's response handling drifted from the
    schema."""
    from nanobrain.library.steps.approval_step import ApprovalStep
    src = inspect.getsource(ApprovalStep._create_approval)
    # Verify the access pattern: body.get("approval"), then .get("id")
    assert 'body.get("approval")' in src
    assert '.get("id")' in src


# ---------------------------------------------------------------------------
# SynonymCacheLookupStep payload contract — probes 536-540
# ---------------------------------------------------------------------------


def _lookup_step_payload_keys() -> set[str]:
    from apecx_integration.composition.steps import synonym_cache
    src = inspect.getsource(synonym_cache.SynonymCacheLookupStep.process)
    m = re.search(r"payload\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m
    return set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))


def test_probe_536_lookup_payload_keys_match_schema() -> None:
    from apecx_integration.control_plane.schemas.api import (
        VerifiedSynonymLookupRequest,
    )
    schema_fields = set(VerifiedSynonymLookupRequest.model_fields.keys())
    payload_keys = _lookup_step_payload_keys()
    extra = payload_keys - schema_fields
    missing = {"source_vocabulary", "target_vocabulary", "query_terms"} - payload_keys
    assert not extra, f"PROBE 536: lookup step sends extra keys: {extra}"
    assert not missing, f"PROBE 536: lookup step missing required keys: {missing}"


def test_probe_537_lookup_empty_query_terms_is_noop() -> None:
    """Empty query_terms must short-circuit without an HTTP call.
    The schema requires min_length=1 — actually posting an empty
    list would 422. The step must catch this BEFORE the HTTP call."""
    from apecx_integration.composition.steps import synonym_cache
    src = inspect.getsource(synonym_cache.SynonymCacheLookupStep.process)
    # Must check for empty query_terms and return early without POSTing
    assert "if not query_terms" in src or "query_terms:" in src
    # Must return the no-op shape (cached_mappings={}, novel_terms=[])
    assert '"cached_mappings": {}' in src or "'cached_mappings': {}" in src


def test_probe_538_lookup_response_safe_match_access() -> None:
    """The response parser uses match.get('result') — not match['result'].
    Any malformed match dict (missing 'result' key) must be handled
    as 'novel', not crash the step."""
    from apecx_integration.composition.steps import synonym_cache
    src = inspect.getsource(synonym_cache.SynonymCacheLookupStep.process)
    assert 'match.get("result")' in src or "match.get('result')" in src


def test_probe_539_lookup_response_canonical_term_field() -> None:
    """When result is non-None, the parser accesses result['canonical_term'].
    Schema must declare canonical_term on VerifiedSynonym."""
    from apecx_integration.control_plane.schemas.entities import VerifiedSynonym
    assert "canonical_term" in VerifiedSynonym.model_fields


def test_probe_540_lookup_query_terms_max_length_500() -> None:
    """The schema caps query_terms at 500. A step posting 1000 terms
    would 422. This probe locks the schema bound so any change to
    the step's batching has to come through this check first."""
    from apecx_integration.control_plane.schemas.api import (
        VerifiedSynonymLookupRequest,
    )
    field_info = VerifiedSynonymLookupRequest.model_fields["query_terms"]
    # max_length is the constraint; pydantic exposes it via metadata
    md = field_info.metadata
    max_lens = [getattr(m, "max_length", None) for m in md]
    assert 500 in max_lens, (
        f"PROBE 540: query_terms max_length drifted from 500: {md}"
    )


# ---------------------------------------------------------------------------
# VerifiedSynonymWritebackStep contract — probes 541-545
# ---------------------------------------------------------------------------


def _writeback_payload_keys() -> set[str]:
    from apecx_integration.composition.steps import synonym_cache
    src = inspect.getsource(
        synonym_cache.VerifiedSynonymWritebackStep.process
    )
    m = re.search(r"payload\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m
    return set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))


def test_probe_541_writeback_payload_keys_match_schema() -> None:
    from apecx_integration.control_plane.schemas.api import (
        CreateVerifiedSynonymRequest,
    )
    schema_fields = set(CreateVerifiedSynonymRequest.model_fields.keys())
    payload_keys = _writeback_payload_keys()
    extra = payload_keys - schema_fields
    assert not extra, f"PROBE 541: writeback sends extra keys: {extra}"
    # Must include all required-shape fields
    required_step_fields = {
        "source_vocabulary", "query_term", "target_vocabulary",
        "canonical_term", "confidence",
    }
    missing = required_step_fields - payload_keys
    assert not missing, f"PROBE 541: writeback missing keys: {missing}"


def test_probe_542_writeback_409_silent_drop() -> None:
    """A 409 Conflict (concurrent run wrote the same mapping first)
    is NOT a workflow error — must be silently dropped from
    'written'. Probe locks this contract via source inspection."""
    from apecx_integration.composition.steps import synonym_cache
    src = inspect.getsource(
        synonym_cache.VerifiedSynonymWritebackStep.process
    )
    assert "httpx.codes.CONFLICT" in src
    assert "already_existed.append" in src


def test_probe_543_writeback_accepts_approved_mappings_shape() -> None:
    """_coerce_input must accept the canonical 'approved_mappings'
    shape verbatim (no rename)."""
    from apecx_integration.composition.steps import synonym_cache
    inst = synonym_cache.VerifiedSynonymWritebackStep.__new__(
        synonym_cache.VerifiedSynonymWritebackStep
    )
    # Bypass __init__; we only need the method, but it references self.name
    inst._name = "test"  # type: ignore[attr-defined]
    inst.name = "test"  # type: ignore[attr-defined]
    out = synonym_cache.VerifiedSynonymWritebackStep._coerce_input(
        inst, {"approved_mappings": [{"query_term": "x", "canonical_term": "y"}]}
    )
    assert out == [{"query_term": "x", "canonical_term": "y"}]


def test_probe_544_writeback_accepts_llm_proposals_shape() -> None:
    """The ApprovalStep passthrough shape uses query_entity / synonym /
    score field names. _coerce_input must rename them to the canonical
    query_term / canonical_term / confidence shape."""
    from apecx_integration.composition.steps import synonym_cache
    inst = synonym_cache.VerifiedSynonymWritebackStep.__new__(
        synonym_cache.VerifiedSynonymWritebackStep
    )
    inst.name = "test"  # type: ignore[attr-defined]
    out = synonym_cache.VerifiedSynonymWritebackStep._coerce_input(
        inst, {
            "llm_proposals": [
                {"query_entity": "EEEV", "synonym": "Eastern Equine", "score": 0.9},
            ]
        }
    )
    assert out == [{
        "query_term": "EEEV",
        "canonical_term": "Eastern Equine",
        "confidence": 0.9,
        "source_run_id": None,
        "comment": None,
    }]


def test_probe_545_writeback_rejects_neither_shape() -> None:
    """Missing both 'approved_mappings' and 'llm_proposals' must
    fail-fast with a clear message, NOT silently treat as empty."""
    from apecx_integration.composition.steps import synonym_cache
    inst = synonym_cache.VerifiedSynonymWritebackStep.__new__(
        synonym_cache.VerifiedSynonymWritebackStep
    )
    inst.name = "test"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="approved_mappings"):
        synonym_cache.VerifiedSynonymWritebackStep._coerce_input(inst, {})


# ---------------------------------------------------------------------------
# Lookup response parsing edge cases — probes 546-549
# ---------------------------------------------------------------------------


def _build_lookup_step_with_mock_transport(handler):
    """Build a SynonymCacheLookupStep with its http_client_factory
    swapped for a mock transport. Bypasses from_config so we can
    drive it through process() directly."""
    from apecx_integration.composition.steps import synonym_cache
    transport = httpx.MockTransport(handler)
    inst = synonym_cache.SynonymCacheLookupStep.__new__(
        synonym_cache.SynonymCacheLookupStep
    )
    inst.name = "test"  # type: ignore[attr-defined]
    inst._control_plane_config = {"base_url": "http://test.invalid"}  # type: ignore[attr-defined]
    inst._source_vocabulary = "user_query"  # type: ignore[attr-defined]
    inst._target_vocabulary = "violin.pathogen_id"  # type: ignore[attr-defined]
    inst._scope = None  # type: ignore[attr-defined]
    inst._http_client_factory = lambda: httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url="http://test.invalid", transport=transport
    )
    return inst


def test_probe_546_lookup_missing_matches_field_raises() -> None:
    """Response without a 'matches' list must fail-fast — not
    silently treat as empty."""
    from apecx_integration.composition.steps import synonym_cache

    async def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    step = _build_lookup_step_with_mock_transport(handler)
    with pytest.raises(ValueError, match="matches"):
        asyncio.run(step.process({"query_terms": ["EEEV"]}))


def test_probe_547_lookup_matches_as_dict_raises() -> None:
    """Response with 'matches' as a non-list (e.g. dict) must
    fail-fast."""
    async def handler(request):
        return httpx.Response(200, json={"matches": {"k": "v"}})

    step = _build_lookup_step_with_mock_transport(handler)
    with pytest.raises(ValueError, match="matches"):
        asyncio.run(step.process({"query_terms": ["EEEV"]}))


def test_probe_548_lookup_match_with_null_result_is_novel() -> None:
    """A match whose 'result' is null means the term is novel —
    must land in novel_terms, not raise."""
    async def handler(request):
        return httpx.Response(200, json={"matches": [
            {"query_term": "EEEV", "result": None},
            {"query_term": "VEEV", "result": None},
        ]})

    step = _build_lookup_step_with_mock_transport(handler)
    out = asyncio.run(step.process({"query_terms": ["EEEV", "VEEV"]}))
    assert out == {"cached_mappings": {}, "novel_terms": ["EEEV", "VEEV"]}


def test_probe_549_lookup_match_with_canonical_term() -> None:
    """A match whose 'result' is a real VerifiedSynonym row must
    land in cached_mappings as {query_term: canonical_term}."""
    async def handler(request):
        body = json.loads(request.content)
        # Echo back something resembling a VerifiedSynonym.
        return httpx.Response(200, json={"matches": [
            {
                "query_term": "EEEV",
                "result": {
                    "id": _VALID_UUID_STR,
                    "canonical_term": "Eastern Equine Encephalitis Virus",
                    # other fields the step doesn't access
                },
            }
        ]})

    step = _build_lookup_step_with_mock_transport(handler)
    out = asyncio.run(step.process({"query_terms": ["EEEV"]}))
    assert out == {
        "cached_mappings": {"EEEV": "Eastern Equine Encephalitis Virus"},
        "novel_terms": [],
    }


# ---------------------------------------------------------------------------
# Cross-component invariants — probes 550-554
# ---------------------------------------------------------------------------


def test_probe_550_all_internal_step_payloads_subset_of_schemas() -> None:
    """Bulk invariant: every step's HTTP payload key-set must be a
    subset of its target schema's fields. A regression in any of
    the three step→schema bridges trips this probe."""
    from apecx_integration.control_plane.schemas.api import (
        CreateApprovalRequest, VerifiedSynonymLookupRequest,
        CreateVerifiedSynonymRequest,
    )
    bridges = [
        (_approval_step_create_payload_keys(), CreateApprovalRequest, "ApprovalStep"),
        (_lookup_step_payload_keys(), VerifiedSynonymLookupRequest, "LookupStep"),
        (_writeback_payload_keys(), CreateVerifiedSynonymRequest, "WritebackStep"),
    ]
    for keys, schema, name in bridges:
        schema_fields = set(schema.model_fields.keys())
        extra = keys - schema_fields
        assert not extra, (
            f"PROBE 550: {name} sends keys not in {schema.__name__}: {extra}"
        )


def test_probe_551_writeback_response_path_matches_schema() -> None:
    """Writeback accesses body['verified_synonym']['id']. The schema
    side must mirror this exactly."""
    from apecx_integration.control_plane.schemas.api import (
        VerifiedSynonymResponse,
    )
    from apecx_integration.control_plane.schemas.entities import VerifiedSynonym
    assert "verified_synonym" in VerifiedSynonymResponse.model_fields
    assert "id" in VerifiedSynonym.model_fields


def test_probe_552_coerce_uuid_accepts_uuid_str_none() -> None:
    """source_run_id can be a UUID, str, or None. The coerce
    helper must accept all three without raising."""
    from apecx_integration.composition.steps.synonym_cache import (
        _coerce_uuid_string,
    )
    assert _coerce_uuid_string(None) is None
    assert _coerce_uuid_string(uuid.UUID(_VALID_UUID_STR)) == _VALID_UUID_STR
    assert _coerce_uuid_string(_VALID_UUID_STR) == _VALID_UUID_STR


def test_probe_553_coerce_uuid_rejects_malformed_string() -> None:
    """Malformed strings must NOT be silently passed through —
    that would fail at the schema validator with a less helpful
    error."""
    from apecx_integration.composition.steps.synonym_cache import (
        _coerce_uuid_string,
    )
    with pytest.raises((ValueError, TypeError)):
        _coerce_uuid_string("not-a-uuid")


def test_probe_554_lookup_step_validates_query_terms_type() -> None:
    """A non-list query_terms input must fail-fast — not silently
    iterate over its characters or worse."""
    from apecx_integration.composition.steps import synonym_cache

    async def handler(request):
        return httpx.Response(200, json={"matches": []})

    step = _build_lookup_step_with_mock_transport(handler)
    with pytest.raises(ValueError, match="query_terms"):
        asyncio.run(step.process({"query_terms": "EEEV"}))  # str, not list
