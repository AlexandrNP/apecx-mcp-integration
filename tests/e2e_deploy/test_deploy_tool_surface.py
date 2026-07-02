"""Deployment e2e — the tool SURFACE boots and reports real content.

Presence-based, not count-based: the live tool count drifts (CLAUDE.md warns three sources disagreed),
so we assert the scientist-facing tools a deployment MUST expose are all registered, and that the two
discovery tools return real content. No LLM/docker needed beyond building the server.
"""

from __future__ import annotations

import asyncio

# Scientist-facing tools any deployment must expose (a subset of the live registry — presence is the
# contract, the exact count is not).
_EXPECTED = {
    "list_workflows",
    "describe_workflow",
    "apecx_capabilities",
    "run_workflow",
    "harmonized_search",
    "inspect_workflow",
    "inspect_run",
    "compose_workflow",
    "database_statistics",
    "infrastructure_status",
    "approve_design",
}


def test_server_registers_scientist_tools(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    missing = _EXPECTED - names
    assert not missing, (
        f"deployment missing scientist tools: {sorted(missing)} (have {sorted(names)})"
    )


def test_list_workflows_returns_real_catalog(call):
    payload = call("list_workflows", {})
    wfs = payload.get("workflows") if isinstance(payload, dict) else payload
    assert wfs, f"list_workflows returned nothing: {str(payload)[:200]}"
    names = {(w.get("workflow_name") or w.get("name")) for w in wfs}
    # list_workflows lists the COMPOSABLE catalog (what run_workflow can also drive by name); the
    # composer-synthesis workflow is a stable member.
    assert "rag_e2e_synthesis_workflow" in names, (
        f"catalog missing a known workflow: {sorted(n for n in names if n)}"
    )


def test_discovery_and_tool_surface_are_disconnected_F8(call, server):
    """PIN of F8 (deployment finding, NOT yet fixed): the flagship `viral_epitope_analysis` is a
    registered TOOL that `run_workflow` runs, but it is INVISIBLE to the discovery tools
    (`list_workflows`/`describe_workflow`), which only see the composer catalog. `apecx_capabilities`
    tells the model to "discover names with list_workflows" — yet the flagship isn't there. This is a
    real discoverability gap for a decision the owner must make (unify the surfaces vs keep separate);
    this test records the CURRENT behavior so a future fix flips it deliberately."""
    tool_names = {t.name for t in asyncio.run(server.list_tools())}
    assert "viral_epitope_analysis" in tool_names  # it IS a runnable tool
    lw = call("list_workflows", {})
    wfs = lw.get("workflows") if isinstance(lw, dict) else lw
    listed = {(w.get("workflow_name") or w.get("name")) for w in wfs}
    assert "viral_epitope_analysis" not in listed  # but NOT in discovery (F8)
    dw = call("describe_workflow", {"name": "viral_epitope_analysis"})
    assert (
        dw.get("error") and "unknown workflow" in dw["error"]
    )  # describe_workflow can't see it either


def test_apecx_capabilities_reports_capabilities(call):
    payload = call("apecx_capabilities", {})
    keys = set(payload) if isinstance(payload, dict) else set()
    assert keys & {"how_to_run", "runnable_now", "backends", "capabilities"}, (
        f"apecx_capabilities missing capability keys: {sorted(keys)[:12]}"
    )


def test_infrastructure_status_reports_backend_roster(call):
    # The live backend roster from the real InfraOrchestrator status() (names them even when down).
    payload = call("infrastructure_status", {})
    blob = str(payload).lower()
    assert any(b in blob for b in ("ollama", "postgres", "redis", "minio")), (
        f"infrastructure_status named no backends: {str(payload)[:200]}"
    )
