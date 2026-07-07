"""Error #1: RheaGenomicAnalysisStep must diagnose the REAL cause of a RHEA-leg failure.

Because G127 makes Workflow.run swallow the inner step exception, the caught error is always a
generic "no workflow_output" ValueError — so the step used to blame "server unreachable" even
when the true cause was a missing client library. These tests pin that the diagnosis text
DIFFERS by actual cause (client-import failure vs server-unreachable vs tool-failed).

The client-import branch is exercised against the REAL environment (the rhea client library is
not importable in the test venv — the exact reproduced condition). The server-unreachable and
tool-failed branches inject a fake `rhea.utils.proxy` (so the import step passes) and a stub
probe (SimpleNamespace duck-typing the ProbeResult attrs the method reads) to route the branch.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types

import pytest

from apecx_integration.composition.steps.rhea_genomic_analysis_step import (
    RheaGenomicAnalysisStep,
)


def _step() -> RheaGenomicAnalysisStep:
    return RheaGenomicAnalysisStep.from_config({"name": "diag_test"})


@pytest.mark.skipif(
    importlib.util.find_spec("rhea") is not None,
    reason="rhea client IS importable here; the client-not-importable branch can't be reproduced",
)
def test_diagnosis_names_client_library_when_rhea_absent():
    """REAL: with the rhea client absent, the cause names the client library, NOT the server."""
    cause = asyncio.run(_step()._diagnose_rhea_failure(ValueError("no workflow_output")))
    assert "client library is not importable" in cause
    assert "rhea.utils.proxy" in cause
    assert "server itself may be healthy" in cause
    # The whole point: it must NOT assert the server is unreachable.
    assert "server at" not in cause


def _inject_importable_rhea(monkeypatch):
    for name in ("rhea", "rhea.utils", "rhea.utils.proxy"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def test_diagnosis_names_server_when_client_ok_but_probe_unhealthy(monkeypatch):
    """Client importable + probe unhealthy -> the cause names the server, not the client."""
    _inject_importable_rhea(monkeypatch)
    from apecx_integration.infrastructure import probes

    async def _unhealthy(*, mcp_url, timeout_s=5.0):
        return types.SimpleNamespace(healthy=False, detail="refused", error="connection refused")

    monkeypatch.setattr(probes, "rhea_mcp_probe", _unhealthy)
    cause = asyncio.run(_step()._diagnose_rhea_failure(ValueError("no workflow_output")))
    assert "MCP server" in cause and "unreachable or degraded" in cause
    assert "connection refused" in cause
    assert "client library is not importable" not in cause


def test_diagnosis_names_tool_when_both_prereqs_ok(monkeypatch):
    """Client importable + probe healthy -> the cause blames neither prereq, names the tool run."""
    _inject_importable_rhea(monkeypatch)
    from apecx_integration.infrastructure import probes

    async def _healthy(*, mcp_url, timeout_s=5.0):
        return types.SimpleNamespace(healthy=True, detail="ok", error=None)

    monkeypatch.setattr(probes, "rhea_mcp_probe", _healthy)
    cause = asyncio.run(_step()._diagnose_rhea_failure(RuntimeError("muscle blew up")))
    assert "both reachable" in cause
    assert "muscle blew up" in cause
