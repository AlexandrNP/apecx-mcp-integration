"""Unit tests for CombinationClassificationStep (pure classification stage)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.combination_classification_step import (
    CombinationClassificationStep,
)

_CANDIDATE = "ACDEFGHIKLMN"
_EPITOPE_A = "QRSTVWYAA"


@pytest.fixture(autouse=True)
def _clean_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBRAIN_LOG_DIR", str(tmp_path / "nanobrain-logs"))
    from nanobrain.core import logging_system

    logging_system._system_log_manager = None
    yield
    logging_system._system_log_manager = None


def _stage(tmp_path: Path) -> CombinationClassificationStep:
    p = tmp_path / "combination_classify.yml"
    p.write_text("name: combination_classify\n", encoding="utf-8")
    return CombinationClassificationStep.from_config(str(p))


def _epitopes() -> list[dict]:
    return [
        {
            "role": "candidate",
            "label": "approved candidate peptide",
            "sequence": _CANDIDATE,
            "start": 10,
            "end": 21,
            "source": "candidate_assessment",
            "recognition_evidence": {"class": "direct evidence"},
            "immunodominance_evidence": None,
            "evidence": {},
        },
        {
            "role": "additional",
            "label": "epitope A",
            "sequence": _EPITOPE_A,
            "start": 40,
            "end": 48,
            "source": "curated record",
            "recognition_evidence": {"class": "direct evidence"},
            "immunodominance_evidence": None,
            "evidence": None,
        },
    ]


def _intake_payload(*, combined: bool = False) -> dict:
    evidence_parts = {
        "structural_reasoning": {
            "available": True,
            "mapped_regions": [{"start": 10, "end": 21}, {"start": 40, "end": 48}],
        },
        "functional_validation": {"residue_level_annotation_available": True},
    }
    if combined:
        evidence_parts["combination_evidence"] = [{"source": "curated", "summary": "reported"}]
    return {
        "evidence_parts": evidence_parts,
        "candidate_parts": {"candidate_released": True},
        "epitopes": _epitopes(),
        "scope_query": "q | epitope_scope: ...",
        "protein": "reference context",
        "design_approval_id": "tok-123",
    }


def _run(step: CombinationClassificationStep, input_data: dict) -> dict:
    return asyncio.run(step.process(input_data))["classify_output"]


def test_terminal_passthrough_is_forwarded_unchanged(tmp_path):
    terminal = {
        "markdown": "# Answer\n\nmiss",
        "control_transfer": {"reason": "needs_prerequisite"},
    }
    out = _run(_stage(tmp_path), {"classify_input": dict(terminal)})
    assert out == terminal


def test_combined_records_classify_combination_level(tmp_path):
    out = _run(_stage(tmp_path), {"classify_input": _intake_payload(combined=True)})
    prelim = out["preliminary"]
    assert prelim["combination_support"]["classification"] == "direct combination-level support"
    # Payload threaded through for the release stage.
    assert out["design_approval_id"] == "tok-123"
    assert out["scope_query"].startswith("q ")


def test_no_combined_records_classifies_epitope_level_only(tmp_path):
    out = _run(_stage(tmp_path), {"classify_input": _intake_payload(combined=False)})
    prelim = out["preliminary"]
    assert prelim["combination_support"]["classification"] == "epitope-level support only"
    assert prelim["immunodominance"]["classification"] == "not evaluated"


def test_emits_readiness_and_all_four_classifications(tmp_path):
    out = _run(_stage(tmp_path), {"classify_input": _intake_payload(combined=True)})
    assert out["readiness"]["epitope_count"] == 2
    assert set(out["preliminary"]) == {
        "epitope_support",
        "structural_placement",
        "combination_support",
        "immunodominance",
    }
    assert (
        out["preliminary"]["structural_placement"]["classification"] == "common-reference support"
    )
