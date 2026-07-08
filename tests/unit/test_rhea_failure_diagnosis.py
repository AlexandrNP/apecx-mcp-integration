"""Error #1: RheaGenomicAnalysisStep must diagnose the REAL cause of a RHEA-leg failure.

Because G127 makes Workflow.run swallow the inner step exception, the caught error is always a
generic "no workflow_output" ValueError — so the step must diagnose the REAL cause. These tests
pin that the diagnosis text DIFFERS by actual cause (server-unreachable vs tool-failed).

NOTE: the apecx RHEA leg is now a THIN HTTP client (no in-process rhea import), so the old
"rhea client library not importable" diagnosis branch — and its test — were retired. The
remaining branches use a stub probe (SimpleNamespace duck-typing the ProbeResult attrs the
method reads) to route server-unreachable vs tool-failed.
"""

from __future__ import annotations

import asyncio
import types

from apecx_integration.composition.steps.rhea_genomic_analysis_step import (
    RheaGenomicAnalysisStep,
)


def _step() -> RheaGenomicAnalysisStep:
    return RheaGenomicAnalysisStep.from_config({"name": "diag_test"})


def test_diagnosis_names_server_when_probe_unhealthy(monkeypatch):
    """Probe unhealthy -> the cause names the server. (The thin HTTP client has no
    'rhea client not importable' case — that diagnosis branch was retired with the
    thin-client migration, so the old client-absent test is gone.)"""
    from apecx_integration.infrastructure import probes

    async def _unhealthy(*, mcp_url, timeout_s=5.0):
        return types.SimpleNamespace(healthy=False, detail="refused", error="connection refused")

    monkeypatch.setattr(probes, "rhea_mcp_probe", _unhealthy)
    cause = asyncio.run(_step()._diagnose_rhea_failure(ValueError("no workflow_output")))
    assert "MCP server" in cause and "unreachable or degraded" in cause
    assert "connection refused" in cause
    assert "client library is not importable" not in cause


def test_diagnosis_names_tool_when_both_prereqs_ok(monkeypatch):
    """Probe healthy -> the cause blames neither prereq, names the tool run."""
    from apecx_integration.infrastructure import probes

    async def _healthy(*, mcp_url, timeout_s=5.0):
        return types.SimpleNamespace(healthy=True, detail="ok", error=None)

    monkeypatch.setattr(probes, "rhea_mcp_probe", _healthy)
    cause = asyncio.run(_step()._diagnose_rhea_failure(RuntimeError("muscle blew up")))
    assert "both reachable" in cause
    assert "muscle blew up" in cause
