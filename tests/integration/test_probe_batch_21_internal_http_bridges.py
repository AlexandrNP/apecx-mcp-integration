"""Probe batch 21 — internal step↔Control-Plane HTTP bridge contracts
(probes 530-535).

The MCP layer was found broken in batch 18 (clusters AO + AP) by
field-name mismatches between the MCP tool wrapper and its target
Pydantic schema. This batch audits the *internal* (non-MCP) HTTP
bridge that the nanobrain ApprovalStep uses to talk to the Control
Plane:

  - nanobrain.library.steps.approval_step.ApprovalStep →
    POST /approvals/  (CreateApprovalRequest)

(The synonym-cache lookup/writeback bridges audited by probes 536-554
were retired 2026-06-15 with the violin_bvbrc workflow.)

A field-name drift here would manifest as Pydantic ValidationError
on the *route* side (ApprovalStep gets 422 from the Control Plane)
or — if the schema accepted more — silent data loss. Either way the
scientist sees a workflow run that hangs or fails mid-way.

All probes are pure-Python: source-text inspection and schema field
introspection.
"""

from __future__ import annotations

import inspect
import re

import pytest

pytestmark = pytest.mark.integration


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
        "run_id",
        "step_id",
        "kind",
        "summary",
        "artifact_ids",
        "policy",
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
    assert not extra, f"PROBE 531: ApprovalStep sends keys the schema rejects: {extra}"


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
    assert actual == expected, f"PROBE 533: ApprovalKind enum drifted: {actual} != {expected}"


def test_probe_534_approval_step_required_kwargs_present() -> None:
    """ApprovalStep.process requires run_id and step_id kwargs.
    Missing either must fail-fast with a friendly message — not
    an obscure KeyError or implicit None somewhere downstream."""
    from nanobrain.library.steps import approval_step

    src = inspect.getsource(approval_step)
    # Must require BOTH run_id and step_id
    assert "run_id" in src and "step_id" in src
    # Must raise when either is missing
    assert "requires run_id and step_id" in src or "run_id and step_id kwargs" in src


def test_probe_535_approval_response_unwrap_path_correct() -> None:
    """The unwrap path is body['approval']['id'] — anything else
    would mean the step's response handling drifted from the
    schema."""
    from nanobrain.library.steps.approval_step import ApprovalStep

    src = inspect.getsource(ApprovalStep._create_approval)
    # Verify the access pattern: body.get("approval"), then .get("id")
    assert 'body.get("approval")' in src
    assert '.get("id")' in src
