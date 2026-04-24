"""Unit tests for the MCP surface server — tool registration +
per-tool delegation to the Control Plane.

Strategy: inject a ``ControlPlaneClient`` backed by
``httpx.ASGITransport(app=create_app(engine=...))`` so the tools
talk to a real (in-process) Control Plane without the network hop.
Mocks-policy-compliant: the Control Plane itself is real (migrated
SQLite), the composer + policy + executor are real test-fixture
instances, just wired through DI rather than deployed.
"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, text

from apecx_integration.composition.approval_policy import ApprovalPolicy
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.executors.local import LocalExecutor
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.mcp_surface.control_plane_client import (
    ControlPlaneClient,
)
from apecx_integration.mcp_surface.server import build_server
from apecx_integration.mcp_surface.tools import _shared

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
)
VIOLIN_WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "violin_bvbrc"
)
DEFAULT_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned

    def invoke(self, messages):
        return _PlaceholderResponse(self.canned)


def _factory(canned: str):
    def _f(**_kwargs):
        return _PlaceholderLLM(canned)

    return _f


COMPOSED_ONLY = textwrap.dedent(
    """\
    ```yaml
    name: mcp_e2e_test_wf
    description: "MCP end-to-end test composition"
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)


@pytest.fixture
def mcp_client(cp_engine: Engine):
    """Build an in-process Control Plane app + point the MCP tools
    at a client wired to it via ASGITransport. Yields the client so
    tests can assert against its state if needed."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._llm_factory = _factory(COMPOSED_ONLY)
    composer._artifact_store = store
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=VIOLIN_WORKFLOW_DIR,
    )
    app = create_app(
        engine=cp_engine,
        composer=composer,
        approval_policy=policy,
        local_executor=executor,
    )
    cp_client = ControlPlaneClient("http://testserver")
    cp_client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    _shared.set_client(cp_client)
    try:
        yield cp_client
    finally:
        asyncio.run(cp_client.close())
        _shared.set_client(None)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_build_server_registers_all_expected_tools():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    expected = {
        "start_workflow",
        "show_diff",
        "execute_workflow",
        "list_pending_approvals",
        "approve",
        "reject",
        "correct",
        "estimate_cost",
        "confirm_allocation",
        "export_hpc_bundle",
        "ingest_hpc_bundle",
    }
    assert expected <= names, f"missing tools: {expected - names}"


def test_build_server_does_not_expose_hpc_submit_or_create_approval():
    """``/hpc/submit`` is still 501 + ``create_approval`` is
    internal. Neither should surface as an MCP tool."""
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "submit" not in names
    assert "hpc_submit" not in names
    assert "create_approval" not in names


# ---------------------------------------------------------------------------
# start_workflow → show_diff → execute_workflow round-trip
# ---------------------------------------------------------------------------


def test_start_workflow_tool_round_trips(mcp_client):
    from apecx_integration.mcp_surface.tools.workflows import start_workflow

    result = asyncio.run(
        start_workflow(
            description="extract pathogen entities",
            user_id="alex",
        )
    )
    assert "run" in result
    assert result["run"]["status"] in {"running", "paused"}
    assert UUID(result["run"]["id"])
    assert UUID(result["generated_workflow_artifact_id"])


def test_show_diff_tool_returns_categorization(mcp_client):
    from apecx_integration.mcp_surface.tools.workflows import (
        show_diff,
        start_workflow,
    )

    started = asyncio.run(
        start_workflow(description="extract pathogen entities", user_id="alex")
    )
    run_id = started["run"]["id"]

    diff = asyncio.run(show_diff(run_id=run_id))
    assert diff["yaml_text"].startswith("name: mcp_e2e_test_wf")
    assert isinstance(diff["categorization"], list)
    assert diff["summary_sentence"]


def test_execute_workflow_tool_reaches_terminal_state(mcp_client):
    from apecx_integration.mcp_surface.tools.workflows import (
        execute_workflow,
        start_workflow,
    )

    started = asyncio.run(
        start_workflow(description="extract pathogen entities", user_id="alex")
    )
    run_id = started["run"]["id"]

    result = asyncio.run(execute_workflow(run_id=run_id))
    assert result["status"] in {"completed", "failed"}
    assert result["run_id"] == run_id


# ---------------------------------------------------------------------------
# HPC tool coverage — estimate → confirm → export → ingest
# ---------------------------------------------------------------------------


def test_hpc_estimate_and_confirm_via_mcp_tools(mcp_client, cp_engine):
    from apecx_integration.mcp_surface.tools.hpc import (
        confirm_allocation,
        estimate_cost,
    )
    from apecx_integration.mcp_surface.tools.workflows import (
        start_workflow,
    )

    started = asyncio.run(
        start_workflow(description="entities", user_id="alex")
    )
    run_id = started["run"]["id"]

    est = asyncio.run(estimate_cost(run_id=run_id))
    assert est["total_core_hours"] >= 0.0

    confirmed = asyncio.run(
        confirm_allocation(
            run_id=run_id,
            confirmed_core_hours=est["total_core_hours"],
        )
    )
    assert confirmed["confirmed"] is True


def test_hpc_export_then_ingest_round_trip(mcp_client, cp_engine, tmp_path):
    """Full HPC tool round-trip via MCP: start → export → simulate
    completion → ingest → Run is COMPLETED."""
    import json as _json

    from apecx_integration.mcp_surface.tools.hpc import (
        export_hpc_bundle,
        ingest_hpc_bundle,
    )
    from apecx_integration.mcp_surface.tools.workflows import (
        start_workflow,
    )

    started = asyncio.run(
        start_workflow(description="entities", user_id="alex")
    )
    run_id = started["run"]["id"]
    # Flip run back to RUNNING — normal flow is
    # start→paused-or-running→approve→running→execute. The
    # ingest route expects a non-terminal state. We don't need
    # a real approval here; update the DB directly.
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE run SET status = 'RUNNING', started_at = :ts "
                "WHERE id = :rid"
            ),
            {"ts": datetime.now(UTC).isoformat(), "rid": run_id},
        )

    bundle_dir = tmp_path / "bundle"
    exported = asyncio.run(
        export_hpc_bundle(
            run_id=run_id,
            target_system="polaris",
            output_directory=str(bundle_dir),
        )
    )
    assert Path(exported["bundle_path"]).is_dir()
    assert "qsub submit.pbs" in exported["submit_command"]

    # Simulate a completed remote run.
    (bundle_dir / "apecx_status.txt").write_text("completed")
    (bundle_dir / "outputs").mkdir(exist_ok=True)
    (bundle_dir / "outputs" / "result.json").write_text(
        _json.dumps({"status": "ok"})
    )

    ingested = asyncio.run(
        ingest_hpc_bundle(bundle_path=str(bundle_dir))
    )
    assert ingested["status"] == "completed"
    assert ingested["run_id"] == run_id


# ---------------------------------------------------------------------------
# Approval tools
# ---------------------------------------------------------------------------


def test_list_pending_approvals_tool(mcp_client):
    from apecx_integration.mcp_surface.tools.approvals import (
        list_pending_approvals,
    )

    result = asyncio.run(list_pending_approvals(user_id="alex"))
    assert isinstance(result.get("approvals"), list)


# ---------------------------------------------------------------------------
# Entry-point / client wiring canaries
# ---------------------------------------------------------------------------


def test_get_client_builds_from_env(monkeypatch):
    from apecx_integration.mcp_surface.tools._shared import get_client, set_client

    set_client(None)
    monkeypatch.setenv("APECX_CONTROL_PLANE_URL", "http://example.invalid")
    client = get_client()
    assert client._base_url == "http://example.invalid"
    set_client(None)
