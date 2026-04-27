"""Probe batch 19 — systematic MCP tool ↔ schema audit (probes 480-504).

Batch 18 found two MCP-to-schema field-name mismatches (clusters AO,
AP) where invocations 100% failed at the MCP layer with Pydantic
ValidationError before reaching the Control Plane. This batch
systematically probes ALL 11 MCP tools end-to-end (with stub clients
that capture the request body) to detect any other instances of
the same bug class.

Tools audited:
  workflows: start_workflow, show_diff, execute_workflow
  approvals: list_pending_approvals, approve, reject, correct
  hpc:       estimate_cost, confirm_allocation, export_hpc_bundle,
             ingest_hpc_bundle

For each tool we verify:
  1. Invoking with valid args does NOT raise ValidationError
     (i.e., the MCP layer correctly builds the request).
  2. The MCP function signature only contains fields the schema
     accepts (no orphan parameters that would silently be dropped
     or rejected).
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Stub-client helpers: capture the request body for each MCP tool
# ---------------------------------------------------------------------------


_VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


class _CapturingClient:
    """A stub ControlPlaneClient that records the request body it
    received and returns a minimal response shape per method.

    Used to verify that the MCP tool successfully constructed the
    request object — the actual HTTP path is not exercised."""

    def __init__(self) -> None:
        self.last_body = None

    @staticmethod
    def _r(d: dict):
        class _R:
            def model_dump(self, mode="json"):  # noqa: ARG002
                return d
        return _R()

    async def start_workflow(self, body):
        self.last_body = body
        return self._r({"run": {"id": _VALID_UUID}, "generated_workflow_artifact_id": _VALID_UUID})

    async def show_yaml_diff(self, body):
        self.last_body = body
        return self._r({"yaml_text": "name: x", "categorization": [], "summary_sentence": ""})

    async def execute_workflow(self, body):
        self.last_body = body
        return self._r({"status": "completed", "run_id": _VALID_UUID})

    async def list_pending_approvals(self, body):
        self.last_body = body
        return self._r({"approvals": []})

    async def approve(self, body):
        self.last_body = body
        return self._r({"approval": {"id": _VALID_UUID}})

    async def reject(self, body):
        self.last_body = body
        return self._r({"approval": {"id": _VALID_UUID}})

    async def correct(self, body):
        self.last_body = body
        return self._r({"approval": {"id": _VALID_UUID}})

    async def estimate_cost(self, body):
        self.last_body = body
        return self._r({
            "total_core_hours": 0.0,
            "per_step_core_hours": {},
            "confidence_interval": [0.0, 0.0],
            "endpoint": "local",
        })

    async def confirm_allocation(self, body):
        self.last_body = body
        return self._r({"run_id": _VALID_UUID, "confirmed_core_hours": 1.0})

    async def export_hpc_bundle(self, body):
        self.last_body = body
        return self._r({"bundle_path": "/tmp/b", "submit_command": "qsub"})

    async def ingest_hpc_bundle(self, body):
        self.last_body = body
        return self._r({"run_id": _VALID_UUID, "status": "completed"})


@pytest.fixture
def stub_client():
    from apecx_integration.mcp_surface.tools._shared import set_client
    client = _CapturingClient()
    set_client(client)  # type: ignore[arg-type]
    yield client
    set_client(None)


# ---------------------------------------------------------------------------
# End-to-end MCP → schema construction probes — 480-490
# ---------------------------------------------------------------------------


def test_probe_480_start_workflow_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.workflows import start_workflow
    out = asyncio.run(start_workflow(
        description="extract entities and look up synonyms",
        user_id="alice",
        preferred_executor="local",
    ))
    assert out["run"]["id"] == _VALID_UUID
    assert stub_client.last_body.description.startswith("extract")


def test_probe_481_show_diff_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.workflows import show_diff
    out = asyncio.run(show_diff(_VALID_UUID))
    assert "yaml_text" in out
    assert str(stub_client.last_body.run_id) == _VALID_UUID


def test_probe_482_execute_workflow_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.workflows import execute_workflow
    out = asyncio.run(execute_workflow(_VALID_UUID))
    assert out["status"] == "completed"


def test_probe_483_list_pending_approvals_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.approvals import (
        list_pending_approvals,
    )
    out = asyncio.run(list_pending_approvals("alice"))
    assert out == {"approvals": []}
    assert stub_client.last_body.user_id == "alice"


def test_probe_484_approve_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.approvals import approve
    out = asyncio.run(approve(_VALID_UUID, comment="LGTM", decided_by="alice"))
    assert "approval" in out
    assert stub_client.last_body.comment == "LGTM"


def test_probe_485_reject_builds_request(stub_client) -> None:
    """Cluster AO regression — pre-fix this raised ValidationError."""
    from apecx_integration.mcp_surface.tools.approvals import reject
    out = asyncio.run(reject(_VALID_UUID, reason="too risky", decided_by="alice"))
    assert "approval" in out
    assert stub_client.last_body.reason == "too risky"


def test_probe_486_correct_builds_request(stub_client) -> None:
    """Cluster AP regression — pre-fix this raised ValidationError."""
    from apecx_integration.mcp_surface.tools.approvals import correct
    out = asyncio.run(correct(
        _VALID_UUID,
        modifications={"replaced": ["a", "b"]},
        decided_by="alice",
    ))
    assert "approval" in out
    assert stub_client.last_body.modifications == {"replaced": ["a", "b"]}


def test_probe_487_estimate_cost_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.hpc import estimate_cost
    out = asyncio.run(estimate_cost(_VALID_UUID))
    assert "total_core_hours" in out


def test_probe_488_confirm_allocation_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.hpc import confirm_allocation
    out = asyncio.run(confirm_allocation(_VALID_UUID, confirmed_core_hours=10.0))
    assert stub_client.last_body.confirmed_core_hours == 10.0


def test_probe_489_export_hpc_bundle_builds_request(stub_client, tmp_path) -> None:
    from apecx_integration.mcp_surface.tools.hpc import export_hpc_bundle
    out = asyncio.run(export_hpc_bundle(
        _VALID_UUID,
        target_system="polaris",
        output_directory=str(tmp_path),
    ))
    assert "bundle_path" in out
    assert stub_client.last_body.target_system == "polaris"


def test_probe_490_ingest_hpc_bundle_builds_request(stub_client) -> None:
    from apecx_integration.mcp_surface.tools.hpc import ingest_hpc_bundle
    out = asyncio.run(ingest_hpc_bundle("/tmp/some/bundle"))
    assert out["status"] == "completed"
    assert stub_client.last_body.bundle_path == "/tmp/some/bundle"


# ---------------------------------------------------------------------------
# Signature-vs-schema field alignment — 491-501
# ---------------------------------------------------------------------------


def _params(func) -> set[str]:
    return set(inspect.signature(func).parameters.keys())


def test_probe_491_start_workflow_params_align() -> None:
    from apecx_integration.mcp_surface.tools.workflows import start_workflow
    from apecx_integration.control_plane.schemas.api import StartWorkflowRequest
    sig = _params(start_workflow)
    schema_fields = set(StartWorkflowRequest.model_fields.keys())
    # Every MCP param (apart from approval_id-style str re-coerce) must
    # map to a schema field
    assert sig <= schema_fields, (
        f"PROBE 491: MCP start_workflow has parameters not in schema: {sig - schema_fields}"
    )


def test_probe_492_show_diff_params_align() -> None:
    from apecx_integration.mcp_surface.tools.workflows import show_diff
    sig = _params(show_diff)
    # show_diff takes run_id which is parsed from str → UUID
    assert sig == {"run_id"}


def test_probe_493_execute_workflow_params_align() -> None:
    from apecx_integration.mcp_surface.tools.workflows import execute_workflow
    sig = _params(execute_workflow)
    assert sig == {"run_id"}


def test_probe_494_list_pending_approvals_params_align() -> None:
    from apecx_integration.mcp_surface.tools.approvals import (
        list_pending_approvals,
    )
    sig = _params(list_pending_approvals)
    assert sig == {"user_id"}


def test_probe_495_approve_params_align() -> None:
    from apecx_integration.mcp_surface.tools.approvals import approve
    from apecx_integration.control_plane.schemas.api import ApproveRequest
    sig = _params(approve)
    schema_fields = set(ApproveRequest.model_fields.keys())
    assert sig <= schema_fields, (
        f"PROBE 495: MCP approve has parameters not in schema: {sig - schema_fields}"
    )


def test_probe_496_reject_params_align() -> None:
    """Cluster AO regression — sig must NOT contain 'comment'."""
    from apecx_integration.mcp_surface.tools.approvals import reject
    from apecx_integration.control_plane.schemas.api import RejectRequest
    sig = _params(reject)
    schema_fields = set(RejectRequest.model_fields.keys())
    assert sig <= schema_fields, (
        f"PROBE 496: MCP reject has parameters not in schema: {sig - schema_fields}"
    )
    assert "comment" not in sig
    assert "reason" in sig


def test_probe_497_correct_params_align() -> None:
    """Cluster AP regression — sig must NOT contain 'corrected_payload'
    or 'comment'."""
    from apecx_integration.mcp_surface.tools.approvals import correct
    from apecx_integration.control_plane.schemas.api import CorrectRequest
    sig = _params(correct)
    schema_fields = set(CorrectRequest.model_fields.keys())
    assert sig <= schema_fields, (
        f"PROBE 497: MCP correct has parameters not in schema: {sig - schema_fields}"
    )
    assert "corrected_payload" not in sig
    assert "comment" not in sig
    assert "modifications" in sig


def test_probe_498_estimate_cost_params_align() -> None:
    from apecx_integration.mcp_surface.tools.hpc import estimate_cost
    sig = _params(estimate_cost)
    assert sig == {"run_id"}


def test_probe_499_confirm_allocation_params_align() -> None:
    from apecx_integration.mcp_surface.tools.hpc import confirm_allocation
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    sig = _params(confirm_allocation)
    schema_fields = set(ConfirmAllocationRequest.model_fields.keys())
    assert sig <= schema_fields


def test_probe_500_export_hpc_bundle_params_align() -> None:
    from apecx_integration.mcp_surface.tools.hpc import export_hpc_bundle
    from apecx_integration.control_plane.schemas.api import (
        ExportHpcBundleRequest,
    )
    sig = _params(export_hpc_bundle)
    schema_fields = set(ExportHpcBundleRequest.model_fields.keys())
    assert sig <= schema_fields


def test_probe_501_ingest_hpc_bundle_params_align() -> None:
    from apecx_integration.mcp_surface.tools.hpc import ingest_hpc_bundle
    from apecx_integration.control_plane.schemas.api import (
        IngestHpcBundleRequest,
    )
    sig = _params(ingest_hpc_bundle)
    schema_fields = set(IngestHpcBundleRequest.model_fields.keys())
    assert sig <= schema_fields


# ---------------------------------------------------------------------------
# Additional invariants — 502-504
# ---------------------------------------------------------------------------


def test_probe_502_server_registers_eleven_tools() -> None:
    """The CLAUDE.md states 11 scientist-facing tools. server.py must
    register every one. If a future PR forgets to register a new tool,
    Claude Desktop won't see it — silent product regression."""
    import inspect as _inspect
    from apecx_integration.mcp_surface import server
    src = _inspect.getsource(server.build_server)
    expected = [
        "start_workflow", "show_diff", "execute_workflow",
        "list_pending_approvals", "approve", "reject", "correct",
        "estimate_cost", "confirm_allocation", "export_hpc_bundle",
        "ingest_hpc_bundle",
    ]
    missing = [t for t in expected if t not in src]
    assert not missing, f"PROBE 502: server.build_server missing tools: {missing}"


def test_probe_503_control_plane_client_has_all_route_methods() -> None:
    """For every MCP tool there must be a matching ControlPlaneClient
    method. A missing client method would fail at runtime with
    AttributeError."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    expected = [
        "start_workflow", "show_yaml_diff", "execute_workflow",
        "list_pending_approvals", "approve", "reject", "correct",
        "estimate_cost", "confirm_allocation", "export_hpc_bundle",
        "ingest_hpc_bundle",
    ]
    missing = [m for m in expected if not hasattr(ControlPlaneClient, m)]
    assert not missing, f"PROBE 503: client missing methods: {missing}"


def test_probe_504_all_request_schemas_forbid_extra_fields() -> None:
    """Every request schema must inherit from _APIBase (extra="forbid").
    A schema that defaults to extra="allow" would let typo'd fields
    silently land — exactly the silent-failure mode this campaign hunts."""
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest, ShowYamlDiffRequest, ExecuteWorkflowRequest,
        ApproveRequest, RejectRequest, CorrectRequest,
        ListPendingApprovalsRequest, EstimateCostRequest,
        ConfirmAllocationRequest, ExportHpcBundleRequest,
        IngestHpcBundleRequest,
    )
    for cls in (
        StartWorkflowRequest, ShowYamlDiffRequest, ExecuteWorkflowRequest,
        ApproveRequest, RejectRequest, CorrectRequest,
        ListPendingApprovalsRequest, EstimateCostRequest,
        ConfirmAllocationRequest, ExportHpcBundleRequest,
        IngestHpcBundleRequest,
    ):
        cfg = getattr(cls, "model_config", {})
        extra = cfg.get("extra") if isinstance(cfg, dict) else cfg.extra
        assert extra == "forbid", (
            f"PROBE 504: {cls.__name__} has extra={extra!r} — must be 'forbid'"
        )
