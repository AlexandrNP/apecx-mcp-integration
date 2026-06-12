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
    shutil.which("mafft") is None or not _bvbrc_reachable(),
    reason="needs MAFFT installed AND BV-BRC reachable",
)


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


def test_in_catalog_and_listed_runnable():
    from apecx_integration.mcp_surface.tools.discovery import list_workflows
    from apecx_integration.mcp_surface.workflow_registry import load_catalog

    names = {e.tool_name for e in load_catalog().workflows}
    assert "viral_conserved_sites" in names

    out = asyncio.run(list_workflows())
    row = next(r for r in out["runnable"] if r["name"] == "viral_conserved_sites")
    assert row["invoke_with"] == "run_workflow"
    # Honest availability: mafft is a binary requirement now checked by the registry.
    assert isinstance(row["available"], bool)
    if shutil.which("mafft") is None:
        assert row["available"] is False
        assert any("mafft" in m for m in row["missing_prerequisites"])


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
