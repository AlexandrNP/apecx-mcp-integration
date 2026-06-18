"""Integration tests for the epitope-combination feasibility workflow."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

pytestmark = pytest.mark.integration

_QUESTION = "epitope combination feasibility assessment"
_PROTEIN = "reference context"
_CANDIDATE = "ACDEFGHIKLMN"
_EPITOPE = "QRSTVWYAA"


@pytest.fixture(autouse=True)
def _clean_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBRAIN_LOG_DIR", str(tmp_path / "nanobrain-logs"))
    from nanobrain.core import logging_system

    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    logging_system._system_log_manager = None
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    logging_system._system_log_manager = None


def _evidence_parts() -> dict:
    residues = list(range(100, 112))
    return {
        "query": _QUESTION,
        "protein": _PROTEIN,
        "conserved_regions": [
            {
                "start": 10,
                "end": 21,
                "length": 12,
                "consensus": _CANDIDATE,
                "mean_identity": 0.99,
            }
        ],
        "structural_records": [{"subject": "pdb:1ABC"}],
        "publications": [{"doi": "10.1/example"}],
        "structural_reasoning": {
            "available": True,
            "mapped_regions": [
                {"start": 10, "end": 21, "consensus": _CANDIDATE, "residues": residues},
                {"start": 40, "end": 48, "consensus": _EPITOPE},
            ],
            "exposed_residues": [{"resi": r} for r in residues],
            "corroborated_residues": [
                {
                    "region_start": 10,
                    "region_end": 21,
                    "motif_index": i,
                    "corroborated": True,
                }
                for i in range(len(_CANDIDATE))
            ],
        },
        "functional_validation": {
            "candidate_source": "structural_exposed_conserved",
            "residue_level_annotation_available": True,
            "coincidences": [{"residue": 101, "type": "reported recognition"}],
        },
        "combination_evidence": [{"source": "curated record", "summary": "reported"}],
    }


def _store_evidence_handle() -> str:
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.schemas.data_shapes import Bundle

    return default_handle_store().put(Bundle(parts=_evidence_parts()))


def _approve() -> str:
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )

    store = get_design_approval_store()
    token = store.request(query=_QUESTION, protein=_PROTEIN)
    store.approve(token)
    return token


def _additional_epitopes() -> list[dict]:
    return [
        {
            "label": "epitope A",
            "sequence": _EPITOPE,
            "start": 40,
            "end": 48,
            "source": "curated record",
            "reported_recognition_evidence": {"class": "direct evidence"},
        }
    ]


def test_workflow_builder_loads_real_nanobrain_workflow():
    from apecx_integration.composition.workflows.epitope_combination_feasibility_assessment.builder import (
        build_epitope_combination_feasibility_assessment_workflow,
    )

    wf = build_epitope_combination_feasibility_assessment_workflow()
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", None)
    assert set(children) == {"intake", "classify", "release", "envelope"}


def test_intake_miss_passes_through_to_envelope():
    """An intake miss (no candidate) must reach the envelope as needs_input UNCHANGED,
    proving the terminal payload survives both the classify and release steps."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "epitope_combination_feasibility_assessment",
            {"additional_epitopes": _additional_epitopes()},
        )
    )
    assert out["status"] == "needs_input", out
    assert "No approved candidate-peptide" in out["markdown"], out


def test_run_workflow_chain_withholds_then_releases_after_approval():
    from apecx_integration.mcp_surface.tools.eo_primitives import approve_design, run_workflow

    evidence_handle = _store_evidence_handle()
    candidate = asyncio.run(
        run_workflow(
            "conserved_epitope_candidate_assessment",
            {"evidence_data_handle": evidence_handle, "design_approval_id": _approve()},
        )
    )
    assert candidate["status"] == "ok", candidate
    assert candidate["data_handle"], candidate

    withheld = asyncio.run(
        run_workflow(
            "epitope_combination_feasibility_assessment",
            {
                "evidence_data_handle": evidence_handle,
                "candidate_assessment_handle": candidate["data_handle"],
                "additional_epitopes": _additional_epitopes(),
            },
        )
    )
    assert withheld["status"] == "needs_input", withheld
    assert withheld["data_handle"], withheld
    dumped = json.dumps(withheld, sort_keys=True)
    assert _CANDIDATE not in dumped
    assert _EPITOPE not in dumped

    match = re.search(r"dapprv-[a-f0-9]+", withheld["markdown"])
    assert match, withheld["markdown"]
    approved = approve_design(match.group(0))
    assert approved.get("status") == "approved", approved

    out = asyncio.run(
        run_workflow(
            "epitope_combination_feasibility_assessment",
            {
                "evidence_data_handle": evidence_handle,
                "candidate_assessment_handle": candidate["data_handle"],
                "additional_epitopes": _additional_epitopes(),
                "design_approval_id": match.group(0),
            },
        )
    )
    assert out["status"] == "ok", out
    assert out["error"] is None
    assert _CANDIDATE in out["markdown"]
    assert _EPITOPE in out["markdown"]
    assert "direct combination-level support" in out["markdown"]
    assert out["data_handle"]
    assert out["data_preview"]["kind"] == "bundle"
