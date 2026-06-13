"""Unit tests for EvidenceReviewSynthesisStep + render_structural_section.

Focus: the structural section is ALWAYS present and deterministic — records when
found, a loud named limitation when not. Synthesis itself is monkeypatched so
these run with no live LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps import evidence_review_synthesis_step as mod
from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    EvidenceReviewSynthesisStep,
    render_structural_section,
)


# ----------------------- pure renderer (no LLM, no construction) -----------------------
def test_render_records_present():
    sec = render_structural_section(
        [
            {"subject": "pdb:1I9G", "content": {"title": "Crystal X"}, "structural_source": "pdb"},
            {"subject": "emdb:EMD-1", "content": {"title": "Cryo Y"}, "structural_source": "emdb"},
        ],
        None,
    )
    assert sec.startswith("## Structural evidence")
    assert "[Globus pdb:1I9G]" in sec and "Crystal X" in sec and "— pdb" in sec
    assert "[Globus emdb:EMD-1]" in sec


def test_render_loud_no_hit():
    note = "No PDB or EMDB structural records were found for 'foo'."
    sec = render_structural_section([], note)
    assert "## Structural evidence" in sec
    assert note in sec
    # NOT just the bare heading — the absence is rendered, not omitted.
    assert sec.strip() != "## Structural evidence (PDB / EMDB)"


def test_render_defensive_both_none():
    sec = render_structural_section(None, None)
    assert "No PDB or EMDB structural records" in sec


# ----------------------- step process() (synthesis monkeypatched) -----------------------
def _stage(tmp_path: Path) -> EvidenceReviewSynthesisStep:
    p = tmp_path / "review.yml"
    p.write_text("name: review_test\n")
    return EvidenceReviewSynthesisStep.from_config(str(p))


def test_appends_structural_records_section(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(mod, "synthesize_response", None, raising=False)
    monkeypatch.setattr(
        "apecx_integration.agents.rag_synthesis.synthesize_response",
        lambda q, **k: "# Evidence\n\nBody [Globus pdb:1I9G].",
    )
    bundle = {
        "query": "chikv E1 epitopes",
        "structural_records": [
            {
                "subject": "pdb:1I9G",
                "content": {"title": "E1 structure"},
                "structural_source": "pdb",
            }
        ],
        "structural_note": None,
    }
    out = asyncio.run(step.process(bundle))
    md = out["markdown"]
    # The contract is now ENFORCED deterministically: the document starts with
    # `# Answer` regardless of what heading the LLM emitted, and the LLM body is
    # preserved within. (Was `startswith("# Evidence")` before the contract guarantee.)
    assert md.startswith("# Answer")
    assert "Body [Globus pdb:1I9G]" in md
    assert "## Structural evidence" in md
    assert "[Globus pdb:1I9G]" in md and "E1 structure" in md


def test_appends_loud_no_hit_section(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(
        "apecx_integration.agents.rag_synthesis.synthesize_response",
        lambda q, **k: "# Evidence\n\nNo structures cited.",
    )
    bundle = {
        "query": "obscure virus xyz",
        "structural_records": [],
        "structural_note": "No PDB or EMDB structural records were found for 'obscure virus xyz'.",
    }
    out = asyncio.run(step.process(bundle))
    md = out["markdown"]
    assert "## Structural evidence" in md
    assert "No PDB or EMDB structural records" in md  # loud no-hit reached the output


def test_synthesis_failure_degrades_loud_keeps_evidence(tmp_path, monkeypatch):
    """RELIABILITY: a synthesis gate failure must NOT discard retrieved evidence.
    The output names the reason and still lists the retrieved publications +
    structural section — never an empty/error result when evidence exists."""
    step = _stage(tmp_path)

    def _boom(q, **k):
        raise ValueError("LLM cited 2 token(s) that were NOT in the retrieval inputs.")

    monkeypatch.setattr("apecx_integration.agents.rag_synthesis.synthesize_response", _boom)
    bundle = {
        "query": "mayaro nsP2 protease",
        "publications": [
            {"doi": "10.1080/07391102.2022.2158941", "title": "Alphavirus protease nsP2 model"}
        ],
        "structural_records": [
            {"subject": "pdb:7XYZ", "content": {"title": "nsP2"}, "structural_source": "pdb"}
        ],
        "structural_note": None,
    }
    out = asyncio.run(step.process(bundle))
    md = out["markdown"]
    # Reason named (loud), evidence preserved, structural section still present.
    assert "Narrative synthesis was withheld" in md
    assert "10.1080/07391102.2022.2158941" in md  # the retrieved publication survived
    assert "## Structural evidence" in md and "[Globus pdb:7XYZ]" in md


def test_missing_query_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"structural_records": []}))
