"""Unit tests for the deterministic evidence sections + full contract assembly (E2-B).

Covers, with NO live LLM (synthesis monkeypatched):
  - ``render_sources_section`` — every citable record listed with its token + title.
  - ``render_followups_section`` — 3–5 questions, gap-seeded first.
  - ``compose_evidence_markdown`` — the five contract sections, in order.
  - ``EvidenceReviewSynthesisStep.process`` — success AND degrade-loud paths both
    yield the five-section contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps._stage_report import append_stage_report
from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    EvidenceReviewSynthesisStep,
    compose_evidence_markdown,
    render_followups_section,
    render_sources_section,
)

_FIVE_HEADERS = [
    "# Answer",
    "## Cross-data reasoning",
    "## Integrated insight",
    "## Sources and evidence",
    "## Follow-up questions",
]


def _realistic_bundle() -> dict:
    """A DataCite-shaped bundle resembling a real structural-query run."""
    return {
        "query": "conserved chikungunya E1 structural epitopes",
        "rag_chunks": [
            {
                "text": "The E1 glycoprotein forms class-II fusion trimers.",
                "source": "chikv_review",
            },
            {"text": "", "source": "empty_should_be_skipped"},  # no text → skipped
        ],
        "bvbrc_genomes": [
            {
                "genome_id": "37124.10",
                "genome_name": "Chikungunya virus strain X",
                "host_name": "Homo sapiens",
            },
            {"genome_name": "no id → skipped"},
        ],
        "violin_mappings": [
            {
                "synonym_id": "VO_0004897",
                "canonical_term": "CHIKV vaccine",
                "query_term": "chikv vaccine",
            },
        ],
        "publications": [
            {
                "doi": "10.1080/07391102.2022.2158941",
                "title": "Alphavirus E1 epitope landscape",
                "authors": ["Doe J", "Roe K"],
                "year": 2022,
                "journal": "J Biomol Struct",
            },
            {"title": "no doi → skipped"},
        ],
        "globus_results": [
            {
                "subject": "pdb:1I9G",
                "content": {
                    "titles": [{"title": "Chikungunya E1-E2 glycoprotein complex"}],
                    "subjects": [{"subject": "alphavirus"}, {"subject": "fusion protein"}],
                },
                "structural_source": "pdb",
            },
            {"content": {"titles": [{"title": "no subject → skipped"}]}},
        ],
        "structural_records": [
            {
                "subject": "pdb:1I9G",
                "content": {"titles": [{"title": "E1-E2 complex"}]},
                "structural_source": "pdb",
            },
        ],
        "structural_note": None,
    }


# --------------------------- Sources ---------------------------
def test_sources_lists_every_citable_record_with_title():
    sec = render_sources_section(_realistic_bundle())
    assert sec.startswith("## Sources and evidence")
    # One per source type, each with its token + a real (non-untitled) title.
    assert "[10.1080/07391102.2022.2158941]" in sec and "Alphavirus E1 epitope landscape" in sec
    assert "[BV-BRC genome 37124.10]" in sec and "Chikungunya virus strain X" in sec
    assert "[VIOLIN VO_0004897]" in sec and "CHIKV vaccine" in sec
    assert "[RAG chunk #1]" in sec and "chikv_review" in sec
    assert "[Globus pdb:1I9G]" in sec and "Chikungunya E1-E2 glycoprotein complex" in sec
    # Records with no citable id are dropped, not rendered as garbage tokens.
    assert "no id → skipped" not in sec
    assert "no doi → skipped" not in sec
    assert "no subject → skipped" not in sec
    # The text-less RAG chunk did not consume a number (only #1 exists).
    assert "[RAG chunk #2]" not in sec


def test_sources_empty_bundle_is_present_and_honest():
    sec = render_sources_section({"query": "q"})
    assert sec.startswith("## Sources and evidence")
    assert "No retrieved records carried a citable identifier" in sec


def test_sources_globus_datacite_title_not_untitled():
    """Regression: DataCite titles live at content.titles[0].title, not a flat key."""
    sec = render_sources_section(
        {
            "globus_results": [
                {"subject": "pdb:7XYZ", "content": {"titles": [{"title": "Real Title"}]}}
            ]
        }
    )
    assert "Real Title" in sec and "(untitled)" not in sec


# --------------------------- Follow-up questions ---------------------------
def test_followups_three_to_five_and_gap_seeded_first():
    bundle = {
        "query": "Mayaro nsP2 protease?",
        "structural_note": "No PDB or EMDB structural records were found for 'Mayaro nsP2'.",
        "structural_records": [],
        "publications": [],
        "bvbrc_genomes": [],
        "violin_mappings": [],
    }
    sec = render_followups_section(bundle["query"], bundle)
    assert sec.startswith("## Follow-up questions")
    items = [ln for ln in sec.splitlines() if ln and ln[0].isdigit()]
    assert 3 <= len(items) <= 5
    # The structural no-hit gap is named first (most actionable).
    assert "expanding the structure search" in items[0]
    # Query text seeds the filler questions; the trailing '?' is stripped before reuse.
    assert "Mayaro nsP2 protease" in sec


def test_followups_always_at_least_three_even_with_no_gaps():
    bundle = {
        "query": "well-covered question",
        "structural_records": [{"subject": "pdb:1"}],
        "structural_note": None,
        "publications": [{"doi": "10.1/x"}],
        "bvbrc_genomes": [{"genome_id": "1.1"}],
        "violin_mappings": [{"synonym_id": "VO_1"}],
    }
    sec = render_followups_section(bundle["query"], bundle)
    items = [ln for ln in sec.splitlines() if ln and ln[0].isdigit()]
    assert len(items) == 3  # no gaps → exactly the 3 query-seeded fillers


# --------------------------- compose (order) ---------------------------
def test_compose_emits_five_sections_in_order():
    bundle = _realistic_bundle()
    append_stage_report(bundle, "context_assembly", 1, "Assembled 5 sources.")
    append_stage_report(bundle, "structural_evidence", 2, "Found 1 structure.")
    narrative = (
        "# Answer\n\nE1 is conserved [Globus pdb:1I9G].\n\n"
        "## Cross-data reasoning\n\nGenomic + structural agree [BV-BRC genome 37124.10].\n\n"
        "## Integrated insight\n\nA combined view [10.1080/07391102.2022.2158941]."
    )
    md = compose_evidence_markdown(narrative, bundle["query"], bundle)
    positions = [md.find(h) for h in _FIVE_HEADERS]
    assert all(p != -1 for p in positions), positions
    assert positions == sorted(positions), f"sections out of order: {positions}"
    # Reasoning trace lands inside cross-data reasoning (before integrated insight).
    trace = md.find("### Reasoning trace")
    assert md.find("## Cross-data reasoning") < trace < md.find("## Integrated insight")
    assert "context_assembly" in md and "structural_evidence" in md
    # Structural section sits between integrated insight and sources.
    assert (
        md.find("## Integrated insight")
        < md.find("## Structural evidence")
        < md.find("## Sources and evidence")
    )


# --------------------------- step process() (synthesis monkeypatched) ---------------------------
def _stage(tmp_path: Path) -> EvidenceReviewSynthesisStep:
    p = tmp_path / "review.yml"
    p.write_text("name: review_test\n")
    return EvidenceReviewSynthesisStep.from_config(str(p))


def test_process_success_path_has_all_five_sections(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(
        "apecx_integration.agents.rag_synthesis.synthesize_response",
        lambda q, **k: (
            "# Answer\n\nConserved [Globus pdb:1I9G].\n\n"
            "## Cross-data reasoning\n\nAgreement [BV-BRC genome 37124.10].\n\n"
            "## Integrated insight\n\nCombined [10.1080/07391102.2022.2158941]."
        ),
    )
    out = asyncio.run(step.process(_realistic_bundle()))
    md = out["markdown"]
    positions = [md.find(h) for h in _FIVE_HEADERS]
    assert all(p != -1 for p in positions), md[:500]
    assert positions == sorted(positions)


def test_process_degrade_loud_still_has_all_five_sections(tmp_path, monkeypatch):
    """RELIABILITY: an LLM gate failure must NOT collapse the contract. The fallback
    body emits the three LLM headings; Sources + Follow-ups are deterministic → all
    five sections present, evidence preserved."""
    step = _stage(tmp_path)

    def _boom(q, **k):
        raise ValueError("LLM cited 2 token(s) that were NOT in the retrieval inputs.")

    monkeypatch.setattr("apecx_integration.agents.rag_synthesis.synthesize_response", _boom)
    out = asyncio.run(step.process(_realistic_bundle()))
    md = out["markdown"]
    positions = [md.find(h) for h in _FIVE_HEADERS]
    assert all(p != -1 for p in positions), md[:500]
    assert positions == sorted(positions)
    assert "Narrative synthesis was withheld" in md
    # Evidence survived the degrade (publication + structure listed in Sources).
    assert "10.1080/07391102.2022.2158941" in md
    assert "[Globus pdb:1I9G]" in md


def test_process_passes_evidence_prompt_override(tmp_path, monkeypatch):
    """The step must pass its evidence output-contract prompt as system_prompt_override
    — proving the chosen seam is wired (NOT the shared synthesis_config prompt)."""
    captured: dict = {}

    def _capture(q, **k):
        captured.update(k)
        return "# Answer\n\nx [Globus pdb:1I9G].\n\n## Cross-data reasoning\n\ny.\n\n## Integrated insight\n\nz."

    step = _stage(tmp_path)
    monkeypatch.setattr("apecx_integration.agents.rag_synthesis.synthesize_response", _capture)
    asyncio.run(step.process(_realistic_bundle()))
    override = captured.get("system_prompt_override")
    assert isinstance(override, str)
    assert (
        "# Answer" in override
        and "## Cross-data reasoning" in override
        and "## Integrated insight" in override
    )


def test_missing_query_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"structural_records": []}))


# --- contract-header guarantee (deterministic, LLM-independent) ---

_FIVE = [
    "# Answer",
    "## Cross-data reasoning",
    "## Integrated insight",
    "## Sources and evidence",
    "## Follow-up questions",
]


def _assert_five_in_order(md: str):
    pos = [md.find(h) for h in _FIVE]
    assert all(p != -1 for p in pos), (pos, md[:500])
    assert pos == sorted(pos), (pos, md[:500])


def test_contract_guaranteed_when_llm_emits_no_headers():
    """Worst case: the LLM returns unstructured prose. All 5 sections still present + ordered."""
    md = compose_evidence_markdown("Unstructured prose about chikungunya.", "chikv", {})
    _assert_five_in_order(md)


def test_contract_guaranteed_when_llm_omits_crossdata():
    md = compose_evidence_markdown("# Answer\n\nFoo.\n\n## Integrated insight\n\nBar.", "chikv", {})
    _assert_five_in_order(md)
    # The injected note is honest/degrade-loud, not fabricated reasoning.
    assert "did not emit a distinct cross-data" in md


def test_contract_preserved_when_llm_emits_all_three():
    md = compose_evidence_markdown(
        "# Answer\n\nA.\n\n## Cross-data reasoning\n\nB.\n\n## Integrated insight\n\nC.",
        "chikv",
        {},
    )
    _assert_five_in_order(md)
    # No spurious duplicate injection.
    assert md.count("## Cross-data reasoning") == 1


def test_degrade_path_stray_headers_dont_break_contract_order():
    """Regression: a citation-gate exception can embed the raw LLM response with
    out-of-order/stray `## ` headers + newlines. The withheld-narrative fallback
    must neutralize them so the 5-section ORDER still holds (surfaced 2026-06-13 by
    a 4B model emitting fullwidth brackets that tripped the gate)."""
    from apecx_integration.composition.steps.evidence_review_synthesis_step import (
        render_evidence_fallback,
    )

    evil = "ValueError: gate failed; response:\n## Integrated insight\nx 【1】\n## Cross-data reasoning\ny"
    md = compose_evidence_markdown(render_evidence_fallback("chikv", [], evil), "chikv", {})
    _assert_five_in_order(md)
