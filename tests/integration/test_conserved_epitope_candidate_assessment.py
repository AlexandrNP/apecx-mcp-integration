"""Integration tests for the conserved_epitope_candidate_assessment workflow."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

pytestmark = pytest.mark.integration

_QUERY = "minimal consensus peptide candidate from conserved-region evidence"
_PROTEIN = "surface glycoprotein"
_PRIMARY = "ACDEFGHIKLMN"


@pytest.fixture(autouse=True)
def _clean_stores():
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )
    from apecx_integration.composition.runtime.execution_locus import (
        ExecutionLocus,
        get_active_locus,
        set_active_locus,
    )
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    # These tests exercise the HITL approval FLOW (needs_input → approve_design → re-call), which
    # is the AGENT-locus (enforcing) behavior. Under the default DESKTOP locus the gate is muted
    # and the payload releases directly (covered by the unit desktop-mute tests + the real MCP probe).
    prev_locus = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    set_active_locus(prev_locus)


def _evidence_parts() -> dict:
    residues = list(range(100, 112))
    return {
        "query": _QUERY,
        "protein": _PROTEIN,
        "conserved_regions": [
            {
                "start": 10,
                "end": 21,
                "length": 12,
                "consensus": _PRIMARY,
                "mean_identity": 0.99,
            }
        ],
        "structural_records": [{"subject": "pdb:1ABC"}],
        "publications": [{"doi": "10.1/example"}],
        "structural_reasoning": {
            "available": True,
            "mapped_regions": [
                {"start": 10, "end": 21, "consensus": _PRIMARY, "residues": residues}
            ],
            "exposed_residues": [{"resi": r} for r in residues],
            "corroborated_residues": [
                {
                    "region_start": 10,
                    "region_end": 21,
                    "motif_index": i,
                    "corroborated": True,
                }
                for i in range(len(_PRIMARY))
            ],
        },
        "functional_validation": {
            "candidate_source": "structural_exposed_conserved",
            "residue_level_annotation_available": True,
            "coincidences": [{"residue": 101, "type": "reported recognition"}],
        },
        "cross_clade_breadth": {
            "available": True,
            "n_clades": 3,
            "alignment_length": 60,
            "pan_clade_regions": [{"start": 10, "end": 21, "length": 12, "consensus": _PRIMARY}],
            "clade_restricted_regions": [],
        },
    }


def _store_handle() -> str:
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.schemas.data_shapes import Bundle

    return default_handle_store().put(Bundle(parts=_evidence_parts()))


def _approve() -> str:
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )

    store = get_design_approval_store()
    token = store.request(query=_QUERY, protein=_PROTEIN)
    store.approve(token)
    return token


def test_workflow_builder_loads_real_nanobrain_workflow():
    from apecx_integration.composition.workflows.conserved_epitope_candidate_assessment.builder import (
        build_conserved_epitope_candidate_assessment_workflow,
    )

    wf = build_conserved_epitope_candidate_assessment_workflow()
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", None)
    assert set(children) == {"assessment", "envelope"}


def test_run_workflow_without_approval_needs_input_and_leaks_no_sequence():
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "conserved_epitope_candidate_assessment",
            {"evidence_data_handle": _store_handle()},
        )
    )
    assert out["status"] == "needs_input", out
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert out["data_handle"]
    assert out["data_preview"]["kind"] == "bundle"
    assert _PRIMARY not in json.dumps(out, sort_keys=True)


def test_run_workflow_with_approval_returns_candidate_assessment():
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "conserved_epitope_candidate_assessment",
            {"evidence_data_handle": _store_handle(), "design_approval_id": _approve()},
        )
    )
    assert out["status"] == "ok", out
    assert out["error"] is None
    assert f"`{_PRIMARY}`" in out["markdown"]
    assert "Cross-clade breadth: pan-clade" in out["markdown"]
    assert out["data_handle"]
    assert out["data_preview"]["kind"] == "bundle"


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


needs_globus = pytest.mark.skipif(
    not _globus_reachable(), reason="needs reachable Globus Search for upstream evidence run"
)


def _rhea_reachable() -> bool:
    """RHEA MCP server on :3001 (the YAML default). The upstream viral_epitope_analysis sequence leg is
    fail-closed RHEA-required when it runs, so a no-RHEA env makes the real upstream run ERROR — skip (not
    fail) when RHEA is absent, mirroring needs_globus/needs_llm (finding EF5)."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 3001), timeout=2):
            return True
    except OSError:
        return False


needs_rhea = pytest.mark.skipif(
    not _rhea_reachable(),
    reason="needs the RHEA MCP server (:3001) for the RHEA-required upstream run (EF5)",
)


@needs_globus
@needs_rhea
def test_real_upstream_handle_can_chain_into_candidate_assessment():
    """Real-data parity: upstream run -> data_handle -> gated follow-up -> approved follow-up."""
    from apecx_integration.mcp_surface.tools.eo_primitives import approve_design, run_workflow

    upstream = asyncio.run(
        run_workflow(
            "viral_epitope_analysis",
            {
                "query": "chikungunya structural polyprotein conserved epitopes",
                "protein": "structural polyprotein",
            },
        )
    )
    assert upstream["status"] == "ok", upstream
    assert upstream["data_handle"], upstream

    withheld = asyncio.run(
        run_workflow(
            "conserved_epitope_candidate_assessment",
            {"evidence_data_handle": upstream["data_handle"]},
        )
    )
    assert withheld["status"] == "needs_input", withheld
    match = re.search(r"dapprv-[a-f0-9]+", withheld["markdown"])
    assert match, withheld["markdown"]
    approved = approve_design(match.group(0))
    assert approved.get("status") == "approved", approved

    out = asyncio.run(
        run_workflow(
            "conserved_epitope_candidate_assessment",
            {
                "evidence_data_handle": upstream["data_handle"],
                "design_approval_id": match.group(0),
            },
        )
    )
    assert out["status"] == "ok", out
    assert "## Minimal consensus peptide candidate" in out["markdown"]
    # the breadth signal flows through from the real upstream handle (pan-clade when multiclade,
    # else "not evaluated" for a single-clade virus) — never silently dropped.
    assert "Cross-clade breadth:" in out["markdown"]
