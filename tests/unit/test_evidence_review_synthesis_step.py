"""Unit tests for EvidenceReviewSynthesisStep + render_structural_section.

Focus: the structural section is ALWAYS present and deterministic — records when
found, a loud named limitation when not. Synthesis itself is monkeypatched so
these run with no live LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.steps import evidence_review_synthesis_step as mod
from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    EvidenceReviewSynthesisStep,
    render_structural_section,
)


@pytest.fixture
def agent_locus():
    """Run a test under AGENT locus (internal synthesis path), restoring the prior locus.

    The default locus is ``desktop`` (host synthesizes → the apecx LLM call is OMITTED), so
    a test that exercises the internal-synthesis branch must opt into ``agent`` explicitly.
    """
    prior = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    try:
        yield
    finally:
        set_active_locus(prior)


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


def test_render_embeds_pymol_visualization():
    """When the PyMOL SASA reasoning produced a PNG, the structural section embeds it."""
    sec = render_structural_section(
        [{"subject": "pdb:2XFB", "content": {"title": "CHIKV E1"}, "structural_source": "pdb"}],
        None,
        reasoning={"available": True, "pdb_id": "2XFB", "visualization_artifact": "/a/2XFB.png"},
    )
    assert "![Epitope surface map — 2XFB](/a/2XFB.png)" in sec


def test_render_surfaces_sasa_failure_reason_not_silent():
    """When SASA was unavailable, the UNDERLYING reason (container traceback) is rendered in a
    fenced block — not a silent generic 'failed'."""
    reason = "PyMOL job failed: ImportError: libGL.so.1\nTraceback (most recent call last):\n  ..."
    sec = render_structural_section(
        [],
        "No structures.",
        reasoning={"available": False, "pdb_id": "2XFB", "note": reason},
    )
    assert "Surface-exposure (SASA) assessment unavailable" in sec
    assert "ImportError: libGL.so.1" in sec  # the REAL reason, not generic
    assert "```" in sec  # fenced (preserves the multi-line traceback)


# ----------------------- step process() (synthesis monkeypatched) -----------------------
def _stage(tmp_path: Path) -> EvidenceReviewSynthesisStep:
    p = tmp_path / "review.yml"
    p.write_text("name: review_test\n")
    return EvidenceReviewSynthesisStep.from_config(str(p))


def test_appends_structural_records_section(tmp_path, monkeypatch, agent_locus):
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


def test_review_feeds_llm_the_bundle_lists(tmp_path, monkeypatch, agent_locus):
    """review hands the bundle's source lists (already the distilled top-N upstream) to the LLM."""
    step = _stage(tmp_path)
    captured: dict = {}

    def _spy(q, **k):
        captured.update(k)
        return "# Answer\n\nbody"

    monkeypatch.setattr("apecx_integration.agents.rag_synthesis.synthesize_response", _spy)
    pubs = [{"doi": "10.1", "title": "p"}]
    asyncio.run(step.process({"query": "chikv", "publications": pubs}))
    assert captured["publications"] == pubs


def test_appends_loud_no_hit_section(tmp_path, monkeypatch, agent_locus):
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


def test_synthesis_failure_degrades_loud_keeps_evidence(tmp_path, monkeypatch, agent_locus):
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


def test_server_always_writes_report_and_structured_data_in_both_loci(tmp_path, monkeypatch):
    """The SERVER always writes the finished report (the desktop 'defer to the host' scaffold
    was removed 2026-06-15 — it produced no durable artifact and the host LLM discarded it). In
    BOTH loci the step returns a complete markdown report AND a structured ``data`` bundle. With
    no reachable LLM it degrades LOUD (deterministic body), still a complete document — never a
    scaffold that tells the host to write the answer."""
    bundle = {
        "query": "chikv E1 epitopes",
        "taxon_id": 37124,
        "protein": "E1",
        "publications": [{"doi": "10.1/abc", "title": "E1 paper"}],
        "conserved_regions": [{"start": 10, "end": 20, "identity": 0.97}],
        "structural_records": [
            {
                "subject": "pdb:2XFB",
                "content": {"title": "E1 structure"},
                "structural_source": "pdb",
            }
        ],
        "structural_note": None,
    }

    def _no_llm(q, **k):
        raise RuntimeError("no LLM reachable")  # force the degrade-loud deterministic path

    monkeypatch.setattr("apecx_integration.agents.rag_synthesis.synthesize_response", _no_llm)

    for locus in (ExecutionLocus.DESKTOP, ExecutionLocus.AGENT):
        set_active_locus(locus)
        out = asyncio.run(_stage(tmp_path).process(bundle))
        md = out["markdown"]
        # A complete report, NOT a scaffold — the user sees this directly.
        assert md.startswith("# Answer"), locus
        assert "ACTION REQUIRED" not in md and "Synthesis is deferred to you" not in md, locus
        assert "## Structural evidence" in md and "[Globus pdb:2XFB]" in md, locus
        assert "10.1/abc" in md, locus
        # STRUCTURED OUTPUT emitted alongside the markdown (the main requirement).
        data = out["data"]
        assert data["kind"] == "bundle"
        parts = data["parts"]
        assert parts["taxon_id"] == 37124 and parts["protein"] == "E1"
        assert parts["counts"]["conserved_regions"] == 1
        assert parts["counts"]["structural_records"] == 1


def test_missing_query_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"structural_records": []}))
