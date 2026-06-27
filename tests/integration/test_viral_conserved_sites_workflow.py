"""viral_conserved_sites workflow (EO-53) — the conserved-sites cascade as a catalog workflow.

Proves the lightweight WorkflowBuilder catalog entry: it builds with real child steps (guards
the WorkflowBuilder 0-child-steps silent failure), is discoverable + honestly gated in
list_workflows, and — gated on MAFFT + BV-BRC — runs end-to-end through the EO `run_workflow`
primitive to a real WorkflowResult.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
import requests

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/genome_feature/"
            f"?eq(taxon_id,{_CHIKV_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_deps = pytest.mark.skipif(
    shutil.which("docker") is None or not _bvbrc_reachable(),
    reason="needs MAFFT installed AND BV-BRC reachable",
)

# RoC-2c needs_input cases need only the mafft prerequisite met (so run_workflow passes its
# availability gate and reaches the param check) — they return BEFORE any BV-BRC/MAFFT call.
needs_mafft = pytest.mark.skipif(shutil.which("docker") is None, reason="needs MAFFT installed")


def test_builder_produces_workflow_with_child_steps():
    # No network: just construct. Guards the WorkflowBuilder 0-child-steps silent failure —
    # a workflow that loads with zero steps would run to {status:'no_first_step'} silently.
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_workflow,
    )

    wf = build_viral_conserved_sites_workflow()
    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
    )
    assert isinstance(children, dict)
    assert set(children) == {"fetch", "align", "conserve", "report", "envelope"}


def test_discovered_and_listed_runnable():
    from apecx_integration.mcp_surface.tools.discovery import list_workflows

    # viral_conserved_sites was RETIRED as a first-class CATALOG tool (2026-06-16, see the catalog
    # NOTE) but remains a DYNAMICALLY DISCOVERED runnable workflow (the epitope tool nests its
    # builder) — so it must still surface in list_workflows as a run_workflow-invokable row.
    out = asyncio.run(list_workflows())
    assert "viral_conserved_sites" in {r["name"] for r in out["runnable"]}
    row = next(r for r in out["runnable"] if r["name"] == "viral_conserved_sites")
    assert row["invoke_with"] == "run_workflow"
    # viral_conserved_sites is DYNAMICALLY discovered (not cataloged), so by design it lists as
    # available:True with NO static prereq gate — a missing backend (here: Docker, for the now
    # container-only MAFFT aligner) surfaces as a LOUD runtime failure, never a silent skip
    # (discovery.py: "a real missing backend surfaces as a loud failure at run"). The container-only
    # Docker requirement is enforced + degrades loud at RUN time — covered by
    # test_local_mafft_align_step.test_no_docker_degrades_loud_not_mock — not by this static listing.
    assert row["available"] is True
    assert row["missing_prerequisites"] == []


@needs_deps
def test_run_workflow_end_to_end():
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "viral_conserved_sites",
            {"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein"},
        )
    )
    assert out["status"] == "ok", out
    assert out["error"] is None
    assert "Conserved sites" in out["markdown"]
    assert out["run_id"]
    # The structured conservation result is carried behind a handle, not in the markdown.
    assert out["data_handle"]


# --------------------------------------------------------------------------- #
# RoC-2c — run_workflow returns needs_input on missing/ill-typed params, BEFORE any backend call.
# (Required params derived from the workflow's OWN step_input_schema, not the catalog.)
# --------------------------------------------------------------------------- #
@needs_mafft
def test_run_workflow_missing_param_returns_needs_input():
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_conserved_sites", {"protein": "E1"}))  # no taxon_id
    assert out["status"] == "needs_input", out
    ct = out["control_transfer"]
    assert ct["reason"] == "missing_param"
    params = ct["next_action"]["params"]
    taxon = next(p for p in params if p["param_name"] == "taxon_id")
    assert taxon["issue"] == "missing"
    assert "harmonized_search" in (taxon["obtain_via"] or "")
    # Did NOT run — no result envelope fields from an actual run.
    assert out["data_handle"] is None


@needs_mafft
def test_run_workflow_illtyped_param_returns_needs_input():
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow("viral_conserved_sites", {"taxon_id": "not-an-int", "protein": "E1"})
    )
    assert out["status"] == "needs_input", out
    params = out["control_transfer"]["next_action"]["params"]
    assert any(p["param_name"] == "taxon_id" and p["issue"] == "ill_typed" for p in params)
