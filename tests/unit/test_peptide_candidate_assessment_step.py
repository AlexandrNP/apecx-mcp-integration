"""Unit tests for the approval-gated peptide candidate assessment step."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apecx_integration.composition.handles.store import HandleNotFound, default_handle_store
from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.schemas.data_shapes import Bundle
from apecx_integration.composition.steps.peptide_candidate_assessment_step import (
    PeptideCandidateAssessmentStep,
)

_QUESTION = "minimal consensus peptide candidate from conserved-region evidence"
_PROTEIN = "surface glycoprotein"
_PRIMARY = "ACDEFGHIKLMN"
_ALT = "QRSTVWYAA"


@pytest.fixture(autouse=True)
def _clean_stores():
    # Pin AGENT locus so the ENFORCEMENT (withheld) tests run where the gate is fail-closed;
    # the DESKTOP-mute release path is pinned by test_desktop_locus_releases_candidate.
    prev_locus = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    get_design_approval_store().clear()
    default_handle_store().clear()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    set_active_locus(prev_locus)


def _stage(tmp_path: Path) -> PeptideCandidateAssessmentStep:
    p = tmp_path / "candidate_assessment.yml"
    p.write_text("name: candidate_assessment\n", encoding="utf-8")
    return PeptideCandidateAssessmentStep.from_config(str(p))


def _evidence_parts() -> dict:
    primary_residues = list(range(100, 112))
    alt_residues = list(range(200, 209))
    return {
        "query": _QUESTION,
        "protein": _PROTEIN,
        "conserved_regions": [
            {
                "start": 10,
                "end": 21,
                "length": 12,
                "consensus": _PRIMARY,
                "mean_identity": 0.99,
            },
            {
                "start": 40,
                "end": 48,
                "length": 9,
                "consensus": _ALT,
                "mean_identity": 0.96,
            },
            {
                "start": 80,
                "end": 83,
                "length": 4,
                "consensus": "AAAA",
                "mean_identity": 1.0,
            },
        ],
        "structural_records": [{"subject": "pdb:1ABC"}],
        "publications": [{"doi": "10.1/example"}],
        "structural_reasoning": {
            "available": True,
            "mapped_regions": [
                {"start": 10, "end": 21, "consensus": _PRIMARY, "residues": primary_residues},
                {"start": 40, "end": 48, "consensus": _ALT, "residues": alt_residues},
            ],
            "exposed_residues": [{"resi": r} for r in primary_residues]
            + [{"resi": alt_residues[0]}],
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
    }


def _approved_token() -> str:
    store = get_design_approval_store()
    token = store.request(query=_QUESTION, protein=_PROTEIN)
    store.approve(token)
    return token


def _run(step: PeptideCandidateAssessmentStep, payload: dict) -> dict:
    out = asyncio.run(step.process(payload))
    return out["assessment_output"]


def test_missing_evidence_source_returns_needs_input(tmp_path):
    out = _run(_stage(tmp_path), {})
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "No structured evidence bundle" in out["markdown"]
    assert _PRIMARY not in out["markdown"]


def test_both_handle_and_inline_bundle_fail_loudly(tmp_path):
    handle = default_handle_store().put(Bundle(parts=_evidence_parts()))
    with pytest.raises(ValueError, match="exactly one evidence source"):
        _run(
            _stage(tmp_path),
            {"evidence_data_handle": handle, "evidence_bundle": {"kind": "bundle", "parts": {}}},
        )


def test_unknown_handle_fails_loudly(tmp_path):
    with pytest.raises(HandleNotFound):
        _run(_stage(tmp_path), {"evidence_data_handle": "missing-handle"})


def test_preview_shape_bundle_gives_actionable_error(tmp_path):
    """The keys-only data_preview ({kind:bundle, parts:[names]}) is rejected with guidance to
    pass the resolvable evidence_data_handle — not the cryptic 'parts must be a dict'."""
    with pytest.raises(ValueError, match="evidence_data_handle"):
        _run(
            _stage(tmp_path),
            {"evidence_bundle": {"kind": "bundle", "parts": ["query", "protein"]}},
        )


def test_desktop_locus_releases_candidate(tmp_path):
    """Under DESKTOP locus the gate is advisory: the candidate peptide is RELEASED with NO
    approval token. The autouse fixture pins AGENT; this flips to DESKTOP."""
    set_active_locus(ExecutionLocus.DESKTOP)
    out = _run(
        _stage(tmp_path), {"evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()}}
    )
    assert "control_transfer" not in out  # released, not a needs_input pause
    assert out["data"]["parts"]["candidate_released"] is True


def test_missing_approval_withholds_all_candidate_sequence_output(tmp_path):
    out = _run(
        _stage(tmp_path), {"evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()}}
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    dumped = json.dumps(out, sort_keys=True)
    assert _PRIMARY not in dumped
    assert _ALT not in dumped
    assert "Candidate peptide sequence output is withheld" in out["markdown"]


def test_approved_token_emits_candidate_assessment(tmp_path):
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()},
            "design_approval_id": _approved_token(),
        },
    )
    assert "control_transfer" not in out
    assert out["data"]["parts"]["candidate_released"] is True
    assert out["data"]["parts"]["candidate"]["sequence"] == _PRIMARY
    assert "## Minimal consensus peptide candidate" in out["markdown"]
    assert f"`{_PRIMARY}`" in out["markdown"]


def test_scope_mismatched_token_remains_withheld(tmp_path):
    store = get_design_approval_store()
    token = store.request(query="different assessment", protein=_PROTEIN)
    store.approve(token)
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()},
            "design_approval_id": token,
        },
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "scope mismatch" in out["markdown"]
    assert _PRIMARY not in json.dumps(out, sort_keys=True)


def test_no_conserved_regions_returns_prerequisite_guidance(tmp_path):
    parts = {**_evidence_parts(), "conserved_regions": []}
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": parts},
            "design_approval_id": _approved_token(),
        },
    )
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "usable conserved-region consensus records: 0" in out["markdown"]
    assert "candidate_released" in json.dumps(out)


def test_candidate_scoring_prefers_conserved_exposed_corroborated_region(tmp_path):
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()},
            "design_approval_id": _approved_token(),
        },
    )
    cand = out["data"]["parts"]["candidate"]
    assert cand["sequence"] == _PRIMARY
    scores = cand["score_components"]
    assert scores["structural_exposure"] == 1.0
    assert scores["cross_structure_support"] == 1.0
    assert cand["reported_recognition_evidence"]["class"] == "direct evidence"


_PAN = "MNPQRSTVWYAC"  # pan-clade region (cols 40-51)
_RESTRICTED = "ACDEFGHIKLMN"  # clade-restricted region (cols 10-21), higher conservation


def _breadth_parts(*, with_breadth: bool) -> dict:
    """Two regions, no structural/functional evidence. The clade-restricted region has HIGHER
    conservation (0.92) so it wins on the 5-way base; the pan-clade region (0.90) can only win
    when breadth is blended in — so a pan-clade victory proves breadth was decisive."""
    parts: dict = {
        "query": _QUESTION,
        "protein": _PROTEIN,
        "conserved_regions": [
            {"start": 10, "end": 21, "length": 12, "consensus": _RESTRICTED, "mean_identity": 0.92},
            {"start": 40, "end": 51, "length": 12, "consensus": _PAN, "mean_identity": 0.90},
        ],
    }
    if with_breadth:
        parts["cross_clade_breadth"] = {
            "available": True,
            "n_clades": 3,
            "alignment_length": 100,
            "pan_clade_regions": [{"start": 40, "end": 51, "length": 12, "consensus": _PAN}],
            "clade_restricted_regions": [{"start": 10, "end": 21, "length": 12}],
        }
    return parts


def test_pan_clade_region_outranks_higher_conservation_clade_restricted(tmp_path):
    # Without breadth the higher-conservation clade-restricted region wins...
    no_breadth = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _breadth_parts(with_breadth=False)},
            "design_approval_id": _approved_token(),
        },
    )
    assert no_breadth["data"]["parts"]["candidate"]["sequence"] == _RESTRICTED
    assert (
        no_breadth["data"]["parts"]["candidate"]["cross_clade_breadth"]["classification"]
        == "not evaluated"
    )

    # ...with breadth, the pan-clade region wins despite its lower conservation.
    with_breadth = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _breadth_parts(with_breadth=True)},
            "design_approval_id": _approved_token(),
        },
    )
    cand = with_breadth["data"]["parts"]["candidate"]
    assert cand["sequence"] == _PAN
    assert cand["cross_clade_breadth"]["classification"] == "pan-clade"
    assert cand["score_components"]["broad_effectiveness"] == 1.0
    assert "Cross-clade breadth: pan-clade" in with_breadth["markdown"]


def test_breadth_absent_does_not_change_the_score(tmp_path):
    # Regression guard: when breadth is not evaluated, total == the exact 5-way base formula.
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()},
            "design_approval_id": _approved_token(),
        },
    )
    cand = out["data"]["parts"]["candidate"]
    assert cand["cross_clade_breadth"]["classification"] == "not evaluated"
    assert cand["score_components"]["broad_effectiveness"] == 0.0
    sc = cand["score_components"]
    expected = round(
        0.40 * sc["conservation"]
        + 0.15 * sc["length_suitability"]
        + 0.20 * sc["structural_exposure"]
        + 0.15 * sc["cross_structure_support"]
        + 0.10 * sc["reported_recognition"],
        4,
    )
    assert cand["score"] == expected


def test_clade_restricted_winner_flags_a_validation_gap(tmp_path):
    parts = {
        "query": _QUESTION,
        "protein": _PROTEIN,
        "conserved_regions": [
            {"start": 10, "end": 21, "length": 12, "consensus": _RESTRICTED, "mean_identity": 0.95},
        ],
        "cross_clade_breadth": {
            "available": True,
            "n_clades": 3,
            "pan_clade_regions": [{"start": 60, "end": 71}],
            "clade_restricted_regions": [{"start": 10, "end": 21}],
        },
    }
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": parts},
            "design_approval_id": _approved_token(),
        },
    )
    cand = out["data"]["parts"]["candidate"]
    assert cand["cross_clade_breadth"]["classification"] == "clade-divergent"
    assert any("not pan-clade conserved" in g for g in out["data"]["parts"]["validation_gaps"])


def test_output_keeps_validation_limitations(tmp_path):
    out = _run(
        _stage(tmp_path),
        {
            "evidence_bundle": {"kind": "bundle", "parts": _evidence_parts()},
            "design_approval_id": _approved_token(),
        },
    )
    lower = out["markdown"].lower()
    assert "not a claim of validation" in lower
    assert "not evaluated in this workflow" in lower
