"""Probe batch 18 — MCP surface, API schemas, provenance hash helpers
+ TypedDict shapes (probes 455-479).

Targets four pure-Python surfaces where silent failures would
manifest as "looks fine in dev, breaks under real users":

  - MCP tools/_shared.parse_run_id (UUID parsing diagnostics)
  - MCP workflows.start_workflow (ExecutorKind validation)
  - control_plane/schemas/api.py (Pydantic extra="forbid" + bounds)
  - control_plane/provenance/recorder helpers (hash-chain primitives)
  - composition/steps/data_unit_schemas.py (cross-step contracts)
  - composition/steps/file_readers.py config validation

All probes are pure-Python — no DB, no FastAPI client, no LLM call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# MCP _shared.parse_run_id — probes 455-458
# ---------------------------------------------------------------------------


def test_probe_455_parse_run_id_accepts_canonical_uuid() -> None:
    from apecx_integration.mcp_surface.tools._shared import parse_run_id

    canonical = "550e8400-e29b-41d4-a716-446655440000"
    parsed = parse_run_id(canonical)
    assert isinstance(parsed, uuid.UUID)
    assert str(parsed) == canonical


def test_probe_456_parse_run_id_rejects_non_string() -> None:
    """A non-string input (None, int) must raise InvalidRunIdError —
    not a generic AttributeError or TypeError that's hard to map
    back to which arg failed."""
    from apecx_integration.mcp_surface.tools._shared import (
        InvalidRunIdError,
        parse_run_id,
    )

    with pytest.raises(InvalidRunIdError):
        parse_run_id(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidRunIdError):
        parse_run_id(12345)  # type: ignore[arg-type]


def test_probe_457_parse_run_id_friendly_error() -> None:
    """The error message must echo the offending input verbatim
    AND show an example UUID so the caller can self-correct."""
    from apecx_integration.mcp_surface.tools._shared import (
        InvalidRunIdError,
        parse_run_id,
    )

    with pytest.raises(InvalidRunIdError) as exc:
        parse_run_id("not-a-uuid")
    msg = str(exc.value)
    assert "not-a-uuid" in msg
    assert "550e8400" in msg  # the canonical example


def test_probe_458_parse_run_id_custom_field_name() -> None:
    """When called with field='approval_id', the error must say
    'approval_id=' not 'run_id=' — the caller used the helper for
    a different field, and the diagnostic must reflect that."""
    from apecx_integration.mcp_surface.tools._shared import (
        InvalidRunIdError,
        parse_run_id,
    )

    with pytest.raises(InvalidRunIdError) as exc:
        parse_run_id("bogus", field="approval_id")
    assert "approval_id=" in str(exc.value)


# ---------------------------------------------------------------------------
# MCP workflows.start_workflow validation — probes 459-461
# ---------------------------------------------------------------------------


def test_probe_459_compose_workflow_rejects_bogus_executor() -> None:
    """preferred_executor must be validated BEFORE Pydantic so the
    error names the bad value. Pre-fix it raised a generic enum
    coercion error; audit §3.10 mandates a friendly hint."""
    import asyncio

    from apecx_integration.mcp_surface.tools.workflows import compose_workflow

    with pytest.raises(ValueError, match="preferred_executor"):
        asyncio.run(
            compose_workflow(
                description="x",
                user_id="u",
                preferred_executor="quantum_supercomputer",
            )
        )


def test_probe_460_valid_executors_set_matches_enum() -> None:
    """_VALID_EXECUTORS must equal every ExecutorKind value. If a
    new executor lands in the enum but the validation set isn't
    updated, the new executor is silently rejected at the MCP
    layer."""
    from apecx_integration.control_plane.schemas.enums import ExecutorKind
    from apecx_integration.mcp_surface.tools.workflows import _VALID_EXECUTORS

    assert {e.value for e in ExecutorKind} == _VALID_EXECUTORS


def test_probe_461_executor_kind_local_is_valid() -> None:
    """The default executor 'local' must be in the valid set —
    sanity check that protects against an enum value being
    renamed without updating defaults."""
    from apecx_integration.control_plane.schemas.enums import ExecutorKind
    from apecx_integration.mcp_surface.tools.workflows import _VALID_EXECUTORS

    assert ExecutorKind.LOCAL.value in _VALID_EXECUTORS


# ---------------------------------------------------------------------------
# API schema strict mode — probes 462-466
# ---------------------------------------------------------------------------


def test_probe_462_start_workflow_rejects_extra_fields() -> None:
    """_APIBase has extra="forbid" — silently ignored typo'd fields
    are exactly the kind of bug where a request "succeeds" but the
    user's intent is dropped."""
    from pydantic import ValidationError

    from apecx_integration.control_plane.schemas.api import StartWorkflowRequest

    with pytest.raises(ValidationError, match="extra"):
        StartWorkflowRequest(
            description="x",
            user_id="u",
            preferred_xecutor="local",  # typo
        )


def test_probe_463_start_workflow_description_non_empty() -> None:
    """An empty description means "compose nothing" — must reject."""
    from pydantic import ValidationError

    from apecx_integration.control_plane.schemas.api import StartWorkflowRequest

    with pytest.raises(ValidationError):
        StartWorkflowRequest(description="", user_id="u")


def test_probe_464_approve_request_rejects_extra() -> None:
    from pydantic import ValidationError

    from apecx_integration.control_plane.schemas.api import ApproveRequest

    with pytest.raises(ValidationError, match="extra"):
        ApproveRequest(
            approval_id=uuid.uuid4(),
            decided_by="u",
            commnt="typo",  # comment misspelled
        )


def test_probe_465_correct_request_accepts_arbitrary_modifications() -> None:
    """modifications is a free-form dict — the reviewer can
    provide any shape; downstream consumers interpret. Must NOT
    reject deeply-nested or unusual values.

    Cluster AP (2026-04-26): the MCP ``correct`` tool was passing
    ``corrected_payload=`` AND ``comment=`` to ``CorrectRequest``,
    both of which were rejected (extra="forbid"), AND
    ``modifications=`` was missing — every invocation 100% broke
    at the MCP layer before reaching the Control Plane. Fixed by
    aligning the MCP signature with the schema.
    """
    from apecx_integration.control_plane.schemas.api import CorrectRequest

    cr = CorrectRequest(
        approval_id=uuid.uuid4(),
        decided_by="u",
        modifications={"deep": {"nested": {"data": [1, 2, 3]}}},
    )
    assert cr.modifications["deep"]["nested"]["data"] == [1, 2, 3]


def test_probe_466_confirm_allocation_requires_run_id() -> None:
    from pydantic import ValidationError

    from apecx_integration.control_plane.schemas.api import ConfirmAllocationRequest

    with pytest.raises(ValidationError):
        ConfirmAllocationRequest(confirmed_core_hours=1.0)  # missing run_id


# ---------------------------------------------------------------------------
# Provenance hash-chain helpers — probes 467-471
# ---------------------------------------------------------------------------


def test_probe_467_canonical_json_deterministic() -> None:
    """Different key orderings of the same dict must produce the
    same canonical string — otherwise hashes diverge for
    semantically-identical payloads."""
    from apecx_integration.control_plane.provenance.recorder import (
        _canonical_json,
    )

    a = _canonical_json({"a": 1, "b": 2, "c": 3})
    b = _canonical_json({"c": 3, "b": 2, "a": 1})
    assert a == b
    # Compact separators (no spaces) to keep canonicalization tight
    assert " " not in a
    # Confirm sorted-keys: 'a' must appear before 'b' before 'c'
    assert a.index('"a"') < a.index('"b"') < a.index('"c"')


def test_probe_468_canonical_timestamp_normalizes_naive_to_utc() -> None:
    """A naive datetime must be assumed UTC (not local). If we
    used local time, hashes computed on different operator
    machines would diverge for the same payload."""
    from apecx_integration.control_plane.provenance.recorder import (
        _canonical_timestamp,
    )

    naive = datetime(2026, 4, 26, 12, 0, 0)
    aware = naive.replace(tzinfo=UTC)
    assert _canonical_timestamp(naive) == _canonical_timestamp(aware)
    # Non-UTC tz must convert to UTC, not be left as-is
    eastern = datetime(2026, 4, 26, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    canonical = _canonical_timestamp(eastern)
    assert canonical.endswith("+00:00") or canonical.endswith("Z")


def test_probe_469_compute_event_hash_deterministic() -> None:
    from apecx_integration.control_plane.provenance.recorder import (
        _compute_event_hash,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )

    rid = uuid.uuid4()
    ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    h1 = _compute_event_hash(
        prev_event_hash=None,
        run_id=rid,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor",
        timestamp=ts,
        payload={"x": 1},
    )
    h2 = _compute_event_hash(
        prev_event_hash=None,
        run_id=rid,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor",
        timestamp=ts,
        payload={"x": 1},
    )
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_probe_470_chain_hash_breaks_on_payload_change() -> None:
    """Tampering with the payload must change the hash. Without
    this property the hash chain provides no integrity guarantee."""
    from apecx_integration.control_plane.provenance.recorder import (
        _compute_event_hash,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )

    rid = uuid.uuid4()
    ts = datetime(2026, 4, 26, tzinfo=UTC)
    base = {
        "prev_event_hash": None,
        "run_id": rid,
        "event_type": ProvenanceEventType.RUN_STARTED,
        "actor": "executor",
        "timestamp": ts,
        "payload": {"x": 1},
    }
    h1 = _compute_event_hash(**base)
    h2 = _compute_event_hash(**{**base, "payload": {"x": 2}})
    assert h1 != h2


def test_probe_471_chain_hash_propagates_prev_hash() -> None:
    """Different prev_event_hash → different current hash. This is
    what gives the chain its integrity property: tampering with
    any earlier event invalidates every subsequent hash."""
    from apecx_integration.control_plane.provenance.recorder import (
        _compute_event_hash,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )

    rid = uuid.uuid4()
    ts = datetime(2026, 4, 26, tzinfo=UTC)
    base = {
        "run_id": rid,
        "event_type": ProvenanceEventType.RUN_STARTED,
        "actor": "executor",
        "timestamp": ts,
        "payload": {"x": 1},
    }
    a = _compute_event_hash(prev_event_hash=None, **base)
    b = _compute_event_hash(prev_event_hash="aa" * 32, **base)
    c = _compute_event_hash(prev_event_hash="bb" * 32, **base)
    assert len({a, b, c}) == 3


# ---------------------------------------------------------------------------
# DataUnit TypedDict shapes — probes 472-475
# ---------------------------------------------------------------------------


def test_probe_472_entity_candidate_keys() -> None:
    """The cross-step contract documented by EntityCandidate must
    contain name/type/confidence — the wrapping function in
    EntityExtractionStep maps to exactly these."""
    from apecx_integration.composition.steps.data_unit_schemas import (
        EntityCandidate,
    )

    keys = set(EntityCandidate.__annotations__)
    assert keys == {"name", "type", "confidence"}


def test_probe_473_llm_synonym_proposal_keys() -> None:
    from apecx_integration.composition.steps.data_unit_schemas import (
        LLMSynonymProposal,
    )

    keys = set(LLMSynonymProposal.__annotations__)
    assert keys == {"query_entity", "synonym", "score"}


def test_probe_474_approved_mapping_optional_fields() -> None:
    """ApprovedMapping is total=False so optional metadata fields
    can be absent. Probe asserts the total flag is set correctly —
    if a future change flips it to total=True (default), every
    minimum-shape mapping breaks at runtime."""
    from apecx_integration.composition.steps.data_unit_schemas import (
        ApprovedMapping,
    )

    assert getattr(ApprovedMapping, "__total__", True) is False


def test_probe_475_step1_output_keys_match_transform() -> None:
    """The Step1Output contract must carry both entities AND
    query_terms — so transforms.entities_to_query_terms can
    pass through pre-flattened query_terms without re-flattening
    entities. If entities is dropped from the contract, the
    "use entities for richer downstream needs" path silently
    breaks."""
    from apecx_integration.composition.steps.data_unit_schemas import (
        Step1Output,
    )

    keys = set(Step1Output.__annotations__)
    assert {"entities", "query_terms"} <= keys


# ---------------------------------------------------------------------------
# MCP-to-schema contract regression probes — probes 476-479
#
# These pin the cluster AO (reject) + AP (correct) bug fixes. Pre-fix the
# MCP tools were passing field names the schemas rejected (and missing
# fields the schemas required) — every reject/correct call from Claude
# Desktop hit a ValidationError at the MCP layer before reaching the
# Control Plane. Tests didn't catch it because nothing exercised the
# MCP-tool→schema bridge end-to-end. These probes do.
# ---------------------------------------------------------------------------


def test_probe_476_mcp_reject_aligns_with_reject_request() -> None:
    """Cluster AO regression. MCP reject() must build a valid
    RejectRequest. Pre-fix it passed comment= which extra-forbidden
    rejected, and reason= was missing — 100% broken."""
    import inspect

    from apecx_integration.mcp_surface.tools.approvals import reject

    sig = inspect.signature(reject)
    # The parameter must be named 'reason' (matches schema field name)
    assert "reason" in sig.parameters
    # And there must NOT be a 'comment' parameter (the bug source)
    assert "comment" not in sig.parameters


def test_probe_477_mcp_correct_aligns_with_correct_request() -> None:
    """Cluster AP regression. MCP correct() must build a valid
    CorrectRequest. Pre-fix it passed corrected_payload= AND
    comment= — both extra-forbidden — and modifications= was
    missing. Lock in the field-name alignment so a future rename
    surfaces here, not in the user's first failed correction."""
    import inspect

    from apecx_integration.mcp_surface.tools.approvals import correct

    sig = inspect.signature(correct)
    assert "modifications" in sig.parameters
    assert "corrected_payload" not in sig.parameters
    assert "comment" not in sig.parameters


def test_probe_478_mcp_reject_actually_builds_request() -> None:
    """End-to-end check: invoking MCP reject() with valid args
    must NOT raise ValidationError. Uses a no-op client to
    avoid hitting the real Control Plane."""
    import asyncio

    from apecx_integration.mcp_surface.tools._shared import set_client
    from apecx_integration.mcp_surface.tools.approvals import reject

    class _StubClient:
        async def reject(self, body):
            class _R:
                def model_dump(self, mode):
                    return {"approval": {"id": str(body.approval_id)}}

            return _R()

    set_client(_StubClient())  # type: ignore[arg-type]
    try:
        out = asyncio.run(
            reject(
                "550e8400-e29b-41d4-a716-446655440000",
                reason="proposed synonyms diverge from canonical",
                decided_by="reviewer@example",
            )
        )
        assert "approval" in out
    finally:
        set_client(None)


def test_probe_479_mcp_correct_actually_builds_request() -> None:
    """Same end-to-end check for correct()."""
    import asyncio

    from apecx_integration.mcp_surface.tools._shared import set_client
    from apecx_integration.mcp_surface.tools.approvals import correct

    class _StubClient:
        async def correct(self, body):
            class _R:
                def model_dump(self, mode):
                    return {
                        "approval": {"id": str(body.approval_id)},
                        "modifications": body.modifications,
                    }

            return _R()

    set_client(_StubClient())  # type: ignore[arg-type]
    try:
        out = asyncio.run(
            correct(
                "550e8400-e29b-41d4-a716-446655440000",
                modifications={"replaced_synonyms": ["EEEV", "VEEV"]},
                decided_by="reviewer@example",
            )
        )
        assert out["modifications"] == {"replaced_synonyms": ["EEEV", "VEEV"]}
    finally:
        set_client(None)
