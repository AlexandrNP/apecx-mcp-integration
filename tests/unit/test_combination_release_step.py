"""Unit tests for CombinationReleaseStep (approval gate + render stage)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.steps.combination_release_step import CombinationReleaseStep

_SCOPE = "scope-A"
_PROTEIN = "reference context"
_CANDIDATE = "ACDEFGHIKLMN"
_EPITOPE_A = "QRSTVWYAA"


@pytest.fixture(autouse=True)
def _clean_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBRAIN_LOG_DIR", str(tmp_path / "nanobrain-logs"))
    from nanobrain.core import logging_system

    logging_system._system_log_manager = None
    # Pin AGENT locus so the ENFORCEMENT (withheld) tests run where the gate is fail-closed;
    # the DESKTOP-mute release path is pinned by test_desktop_locus_releases_without_approval.
    prev_locus = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    get_design_approval_store().clear()
    yield
    get_design_approval_store().clear()
    set_active_locus(prev_locus)
    logging_system._system_log_manager = None


def _stage(tmp_path: Path) -> CombinationReleaseStep:
    p = tmp_path / "combination_release.yml"
    p.write_text("name: combination_release\n", encoding="utf-8")
    return CombinationReleaseStep.from_config(str(p))


def _epitopes() -> list[dict]:
    return [
        {
            "role": "candidate",
            "label": "approved candidate peptide",
            "sequence": _CANDIDATE,
            "start": 10,
            "end": 21,
            "source": "candidate_assessment",
        },
        {
            "role": "additional",
            "label": "epitope A",
            "sequence": _EPITOPE_A,
            "start": 40,
            "end": 48,
            "source": "curated record",
        },
    ]


def _classified_payload(*, approval_id=None) -> dict:
    return {
        "epitopes": _epitopes(),
        "evidence_parts": {},
        "candidate_parts": {"approval": {"token": "prior-tok"}},
        "scope_query": _SCOPE,
        "protein": _PROTEIN,
        "design_approval_id": approval_id,
        "readiness": {"epitope_count": 2},
        "preliminary": {
            "epitope_support": [
                {
                    "label": "approved candidate peptide",
                    "classification": "approved candidate peptide",
                    "basis": "released by the candidate-peptide assessment workflow",
                },
                {
                    "label": "epitope A",
                    "classification": "direct epitope-level support",
                    "basis": "epitope carries direct reported source support",
                },
            ],
            "structural_placement": {"classification": "common-reference support"},
            "combination_support": {
                "classification": "direct combination-level support",
                "basis": "records present",
                "record_count": 1,
            },
            "immunodominance": {
                "classification": "not evaluated",
                "basis": "none",
                "record_count": 0,
            },
        },
    }


def _approved_token(query: str = _SCOPE, protein: str = _PROTEIN) -> str:
    store = get_design_approval_store()
    token = store.request(query=query, protein=protein)
    store.approve(token)
    return token


def _run(step: CombinationReleaseStep, input_data: dict) -> dict:
    return asyncio.run(step.process(input_data))["release_output"]


def test_terminal_passthrough_is_forwarded_unchanged(tmp_path):
    terminal = {
        "markdown": "# Answer\n\nmiss",
        "control_transfer": {"reason": "needs_prerequisite"},
    }
    out = _run(_stage(tmp_path), {"release_input": dict(terminal)})
    assert out == terminal


def test_no_approval_withholds_all_epitope_sequences(tmp_path):
    out = _run(_stage(tmp_path), {"release_input": _classified_payload(approval_id=None)})
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert out["data"]["parts"]["combination_released"] is False
    dumped = json.dumps(out, sort_keys=True)
    assert _CANDIDATE not in dumped
    assert _EPITOPE_A not in dumped
    assert "Epitope sequences" in out["markdown"]
    token = out["data"]["parts"]["approval"]["token"]
    assert token.startswith("dapprv-")


def test_scope_mismatched_token_remains_withheld(tmp_path):
    bad = _approved_token(query="a different request")
    out = _run(_stage(tmp_path), {"release_input": _classified_payload(approval_id=bad)})
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "scope mismatch" in out["markdown"]
    assert _CANDIDATE not in json.dumps(out, sort_keys=True)


def test_desktop_locus_releases_without_approval(tmp_path):
    """Under DESKTOP locus the gate is advisory: the combination is RELEASED (epitope sequences
    present) with NO approval token. The autouse fixture pins AGENT; this flips to DESKTOP."""
    set_active_locus(ExecutionLocus.DESKTOP)
    out = _run(_stage(tmp_path), {"release_input": _classified_payload()})  # no approval_id
    assert "control_transfer" not in out  # released, not a needs_input pause
    assert out["data"]["parts"]["combination_released"] is True
    md = out["markdown"]
    assert _CANDIDATE in md and _EPITOPE_A in md  # sequences released


def test_approved_token_emits_combination_assessment(tmp_path):
    token = _approved_token()
    out = _run(_stage(tmp_path), {"release_input": _classified_payload(approval_id=token)})
    assert "control_transfer" not in out
    parts = out["data"]["parts"]
    assert parts["combination_released"] is True
    assert parts["combination_support"]["classification"] == "direct combination-level support"
    md = out["markdown"]
    assert _CANDIDATE in md
    assert _EPITOPE_A in md
    for header in (
        "## Summary",
        "## Epitopes",
        "## Evidence",
        "## Validation gaps",
        "## Limitations",
    ):
        assert header in md
    assert "combined sequence" in md.lower()
    assert "linker" in md.lower()
