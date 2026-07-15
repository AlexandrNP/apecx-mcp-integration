"""Unit tests for CombinationIntakeStep (load/validate stage)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apecx_integration.composition.handles.store import HandleNotFound, default_handle_store
from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.schemas.data_shapes import Bundle
from apecx_integration.composition.steps.combination_intake_step import CombinationIntakeStep

_CANDIDATE = "ACDEFGHIKLMN"
_EPITOPE_A = "QRSTVWYAA"
_EPITOPE_B = "MNPQRSTVWY"


@pytest.fixture(autouse=True)
def _clean_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBRAIN_LOG_DIR", str(tmp_path / "nanobrain-logs"))
    from nanobrain.core import logging_system

    logging_system._system_log_manager = None
    get_design_approval_store().clear()
    default_handle_store().clear()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    logging_system._system_log_manager = None


def _stage(tmp_path: Path) -> CombinationIntakeStep:
    p = tmp_path / "combination_intake.yml"
    p.write_text("name: combination_intake\n", encoding="utf-8")
    return CombinationIntakeStep.from_config(str(p))


def _candidate_parts(*, released: bool = True) -> dict:
    return {
        "candidate_released": released,
        "candidate": {
            "sequence": _CANDIDATE,
            "source_region": {"start": 10, "end": 21, "length": 12},
            "reported_recognition_evidence": {"class": "direct evidence"},
        },
        "approval": {"scope_query": "q", "protein": "reference context"},
    }


def _additional_epitopes() -> list[dict]:
    return [
        {
            "label": "epitope A",
            "sequence": _EPITOPE_A,
            "start": 40,
            "end": 48,
            "source": "curated record",
            "reported_recognition_evidence": {"class": "direct evidence"},
        },
        {
            "label": "epitope B",
            "sequence": _EPITOPE_B,
            "coordinates": {"start": 70, "end": 79},
            "source": "source record",
        },
    ]


def _run(step: CombinationIntakeStep, payload: dict) -> dict:
    return asyncio.run(step.process(payload))["intake_output"]


def test_missing_candidate_source_returns_needs_input(tmp_path):
    out = _run(_stage(tmp_path), {"additional_epitopes": _additional_epitopes()})
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "No approved candidate-peptide" in out["markdown"]
    assert _CANDIDATE not in json.dumps(out, sort_keys=True)


def test_both_candidate_handle_and_inline_bundle_fail_loudly(tmp_path):
    handle = default_handle_store().put(Bundle(parts=_candidate_parts()))
    with pytest.raises(ValueError, match="exactly one candidate assessment source"):
        _run(
            _stage(tmp_path),
            {
                "candidate_assessment_handle": handle,
                "candidate_assessment_bundle": {"kind": "bundle", "parts": _candidate_parts()},
                "additional_epitopes": _additional_epitopes(),
            },
        )


def test_unknown_candidate_handle_fails_loudly(tmp_path):
    with pytest.raises(HandleNotFound):
        _run(
            _stage(tmp_path),
            {
                "candidate_assessment_handle": "missing-handle",
                "additional_epitopes": _additional_epitopes(),
            },
        )


def test_preview_shape_candidate_bundle_gives_actionable_error(tmp_path):
    """The keys-only data_preview ({kind:bundle, parts:[names]}) passed as an inline bundle is
    rejected with guidance to pass the resolvable handle — not the cryptic 'parts must be a dict'."""
    with pytest.raises(ValueError, match="evidence_data_handle"):
        _run(
            _stage(tmp_path),
            {
                "candidate_assessment_bundle": {"kind": "bundle", "parts": ["candidate_released"]},
                "additional_epitopes": _additional_epitopes(),
            },
        )


def test_unreleased_candidate_returns_prerequisite(tmp_path):
    out = _run(
        _stage(tmp_path),
        {
            "candidate_assessment_bundle": {
                "kind": "bundle",
                "parts": _candidate_parts(released=False),
            },
            "additional_epitopes": _additional_epitopes(),
        },
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "no released candidate" in out["markdown"].lower()
    assert out["data"]["parts"]["combination_released"] is False
    assert _CANDIDATE not in json.dumps(out, sort_keys=True)


def test_missing_additional_epitopes_returns_prerequisite(tmp_path):
    out = _run(
        _stage(tmp_path),
        {"candidate_assessment_bundle": {"kind": "bundle", "parts": _candidate_parts()}},
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "additional_epitopes must be a non-empty list" in out["markdown"]
    assert _CANDIDATE not in json.dumps(out, sort_keys=True)


def test_epitope_count_limit_is_enforced(tmp_path):
    epitopes = [
        {"label": f"epitope {i}", "sequence": "ACDEFG", "start": i * 10, "end": i * 10 + 5}
        for i in range(1, 7)
    ]
    out = _run(
        _stage(tmp_path),
        {
            "candidate_assessment_bundle": {"kind": "bundle", "parts": _candidate_parts()},
            "additional_epitopes": epitopes,
        },
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "too many epitopes supplied" in out["markdown"]


def test_happy_path_emits_normalized_payload_without_markdown(tmp_path):
    out = _run(
        _stage(tmp_path),
        {
            "candidate_assessment_bundle": {"kind": "bundle", "parts": _candidate_parts()},
            "additional_epitopes": _additional_epitopes(),
            "design_approval_id": "tok-123",
        },
    )
    # A non-terminal intake payload MUST NOT carry the terminal marker, or downstream
    # steps would forward it unprocessed.
    assert "markdown" not in out
    assert "control_transfer" not in out
    assert len(out["epitopes"]) == 3  # candidate + 2 additional
    assert out["epitopes"][0]["role"] == "candidate"
    assert out["design_approval_id"] == "tok-123"
    assert "epitope_scope:" in out["scope_query"]
    assert out["protein"] == "reference context"
