"""Surface-lock: the EXACT set of MCP tools the server exposes.

This is the regression guard the `analyze_viral_immunology` incident needed — a
standalone `server.tool()` was added that shadowed the proper workflow and nobody
noticed because nothing pinned the surface. Any change to the exposed tool set
(static primitive OR catalog workflow) must update ``EXPECTED`` here DELIBERATELY,
which forces a review against the Layer-1 design
(``docs/external_orchestration_design.md`` §4: primitives, not super-tools;
operational tools out of the agentic loop; ``docs/layer1_surface_trim_plan.md``).

If this test fails: do NOT just sync the literal. Ask whether the new tool belongs
on the Layer-1 surface at all, or whether it should be a discoverable catalog
workflow (run via ``run_workflow``) or an operational tool off the agentic wire.
"""

from __future__ import annotations

import asyncio

from apecx_integration.mcp_surface.server import build_server

# Layer-1 agentic primitives + meta + the workflow HITL gate (13 static).
EXPECTED_STATIC = {
    # orchestration primitives
    "list_workflows",
    "describe_workflow",
    "inspect_workflow",
    "run_workflow",
    "run_workflow_streaming",
    "inspect_run",
    "apecx_context",
    "apecx_capabilities",
    # single composer primitive (replaced start_workflow/show_diff/execute_workflow)
    "compose_workflow",
    # canonical retrieval primitive
    "harmonized_search",
    # the viral_epitope_analysis workflow's HITL design gate
    "approve_design",
    # meta / navigation
    "database_statistics",
    "infrastructure_status",
}

# Catalog workflows — each registers as one MCP tool (run via run_workflow too).
EXPECTED_CATALOG = {
    "rhea_muscle_alignment",
    "viral_conserved_sites",
    "viral_conserved_sites_muscle",
    "viral_epitope_analysis",
    # Promoted from filesystem discovery via the catalog `promote_discovered:` list (no
    # hand-written entry) — a product workflow exposed as a first-class {query} tool so a
    # model calls it directly instead of via list_workflows → run_workflow. Routes THROUGH
    # run_workflow (not a shadow tool — same execution + gating). 2026-06-15.
    "rag_e2e_synthesis",
}

EXPECTED = EXPECTED_STATIC | EXPECTED_CATALOG

# Tools deliberately RETIRED / never on the agentic surface — assert they stay off.
FORBIDDEN = {
    "synthesize_query",  # retired 2026-06-15 (super-tool, legacy local-CSV retrieval)
    "start_workflow",  # folded into compose_workflow
    "show_diff",
    "execute_workflow",
    "list_pending_approvals",  # operational control-plane — off the agentic loop
    "approve",
    "reject",
    "correct",
    "estimate_cost",  # HPC operational — to become one hpc_export workflow
    "confirm_allocation",
    "export_hpc_bundle",
    "ingest_hpc_bundle",
    "analyze_viral_immunology",  # retired — use viral_epitope_analysis
    "analyze_eeev_epitopes",
    "query_globus_search",  # raw free-text — superseded by harmonized_search
    "query_vaccines",
    "query_pathogens",
    "query_genes",
    "query_bvbrc_genomes",
}


def _registered_tool_names() -> set[str]:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


def test_mcp_surface_is_exactly_the_layer1_set():
    names = _registered_tool_names()
    assert names == EXPECTED, (
        f"MCP surface drifted. Added: {sorted(names - EXPECTED)}; "
        f"removed: {sorted(EXPECTED - names)}. Update EXPECTED only after checking "
        f"the new tool belongs on the Layer-1 surface (see this module's docstring)."
    )


def test_retired_tools_stay_off_the_surface():
    names = _registered_tool_names()
    leaked = names & FORBIDDEN
    assert not leaked, f"retired/forbidden tools are exposed again: {sorted(leaked)}"


def test_surface_size_stays_lean():
    # Layer-1 is ~13 static primitives + catalog workflows. A jump signals
    # super-tools / operational tools creeping back onto the agentic wire.
    names = _registered_tool_names()
    static = names - EXPECTED_CATALOG
    assert len(static) <= 14, f"static surface grew to {len(static)}: {sorted(static)}"
