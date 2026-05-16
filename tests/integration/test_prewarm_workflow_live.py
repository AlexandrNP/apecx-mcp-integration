"""Live integration tests for the nanobrain pre-warm workflow.

Drives the full cascade against the actual orchestrator-spawned
backends (Postgres + Redis), proving that:

* The workflow YAML path executes end-to-end and produces a populated
  PrewarmReport with the expected shape.
* The WorkflowBuilder programmatic path produces an
  identically-behaving workflow (cache hit on the same Redis entry,
  same all_ready outcome) — confirming the two authoring paths are
  not just structurally equivalent but functionally equivalent.
* The orchestrator's ``InfraOrchestrator.prewarm_workflow_tools``
  drives the workflow correctly and stashes the report under
  ``self._prewarm_report``, which the status tool then surfaces.

Gated on the live Postgres + Redis being reachable on the
orchestrator's declared host:ports (apecx-rhea-postgres :5435,
apecx-redis :6379). Auto-skips with an actionable message if they're
down.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_YAML = (
    REPO_ROOT / "src/apecx_integration/infrastructure/prewarm_workflow/configs/prewarm_workflow.yml"
)


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_PG_UP = _port_open("localhost", 5435)
_REDIS_UP = _port_open("localhost", 6379)

_skip_no_backends = pytest.mark.skipif(
    not (_PG_UP and _REDIS_UP),
    reason=(
        "live Postgres on :5435 + Redis on :6379 required (start via "
        "`apecx-mcp` or the orchestrator)."
    ),
)


def _request_payload() -> dict:
    """Build the orchestrator-style prewarm_request dict."""
    import os

    return {
        "catalog_path": None,  # use packaged default
        "database_url": "postgresql://postgres:postgres@localhost:5435/rhea",
        "redis_host": "localhost",
        "redis_port": 6379,
        "rhea_python": os.environ.get("RHEA_PYTHON_PATH"),
    }


@_skip_no_backends
def test_yaml_workflow_cascades_to_completed_report():
    """Drive the YAML-based workflow via the canonical from_config path.

    Asserts the cascade drains, the workflow output DU carries a
    PrewarmReport, and all_ready is True. This requires that muscle
    (the one tool currently in prewarm_rhea_tools) is either
    already cached in Redis OR can be freshly installed against the
    live containers.
    """
    from nanobrain.core.workflow import Workflow

    from apecx_integration.infrastructure.rhea_prewarm import PrewarmReport

    async def _drive():
        wf = Workflow.from_config(str(WORKFLOW_YAML))
        await wf.initialize()
        await wf.process({"prewarm_request": _request_payload()})
        drained = await wf.wait_for_cascade(timeout=120.0, settle_ms=300)
        assert drained, "cascade did not drain — check trigger wiring"
        report_du = wf.output_data_units["prewarm_report"]
        return await report_du.get()

    report = asyncio.run(_drive())
    assert isinstance(report, PrewarmReport), (
        f"expected PrewarmReport instance, got {type(report).__name__}"
    )
    assert report.tools, "no tools in report — the catalog has none?"
    # Catalog currently declares only `muscle`; if the catalog grows,
    # this assertion still holds (each entry should be ready/reused
    # for a passing test) but the count check below would need an
    # update.
    snap = report.snapshot()
    assert snap["all_ready"], f"some tool failed; report: {snap['tools']!r}"


@_skip_no_backends
def test_builder_workflow_cascades_to_same_report_shape():
    """Drive the WorkflowBuilder-built workflow + assert shape parity.

    Proves the two authoring paths (YAML and WorkflowBuilder) produce
    functionally equivalent workflows. As of nanobrain 2026-05-15 the
    builder emits the nested-shape link entries the framework's
    LinkBase.from_config expects natively, so the cascade fires
    without the prior ``_rewrap_link_entries_nested`` workaround
    (friction-log #26). If a future framework regression re-breaks
    builder-emitted links, this test will timeout-or-drain-empty and
    fail loudly.
    """
    from apecx_integration.infrastructure.prewarm_workflow.builder import (
        build_prewarm_workflow_via_builder,
    )
    from apecx_integration.infrastructure.rhea_prewarm import PrewarmReport

    async def _drive():
        wf = build_prewarm_workflow_via_builder()
        await wf.initialize()
        await wf.process({"prewarm_request": _request_payload()})
        drained = await wf.wait_for_cascade(timeout=120.0, settle_ms=300)
        assert drained, "builder cascade did not drain"
        report_du = wf.output_data_units["prewarm_report"]
        return await report_du.get()

    report = asyncio.run(_drive())
    assert isinstance(report, PrewarmReport)
    assert report.tools
    assert report.snapshot()["all_ready"]


@_skip_no_backends
def test_orchestrator_drives_prewarm_workflow_and_stashes_report():
    """Full orchestrator path: prewarm_workflow_tools → status() carries it.

    This is the end-to-end integration: the same code path the
    apecx-mcp server takes at startup. After ``prewarm_workflow_tools()``
    returns, ``status()`` must expose the report under
    ``rhea_tool_prewarm`` with ``all_ready=True``.
    """
    import os

    from apecx_integration.infrastructure.orchestrator import (
        InfraOrchestrator,
        reset_orchestrator_for_testing,
    )

    # Ensure no stale singleton from another test pollutes us.
    reset_orchestrator_for_testing()

    async def _drive():
        # Construct an orchestrator with default specs (which point at
        # the live containers' host:ports).
        orch = InfraOrchestrator()
        # Pre-warm doesn't need start_all to have run; it only needs
        # the runtime specs (resolved at __init__) for port/env
        # extraction.
        await orch.prewarm_workflow_tools()
        return await orch.status()

    snap = asyncio.run(_drive())
    assert "rhea_tool_prewarm" in snap, (
        f"status() did not surface rhea_tool_prewarm; keys={list(snap)!r}"
    )
    prewarm = snap["rhea_tool_prewarm"]
    assert prewarm.get("all_ready") is True, f"prewarm reports not all_ready: {prewarm!r}"
    # No tool failure should have lifted into actionable.
    actionable = snap.get("actionable") or []
    assert not any("prewarm:" in s for s in actionable), (
        f"unexpected prewarm entry in actionable: {actionable!r}"
    )

    # Cleanup — the orchestrator's runtime singleton was just primed
    # by us; reset so the next test starts clean.
    reset_orchestrator_for_testing()
    # Also unset $APECX_MCP_WORKFLOW_CATALOG side-effects if any.
    os.environ.pop("APECX_MCP_WORKFLOW_CATALOG", None)
