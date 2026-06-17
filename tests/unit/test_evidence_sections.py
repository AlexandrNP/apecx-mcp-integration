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

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.steps._stage_report import append_stage_report
from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    EvidenceReviewSynthesisStep,
    compose_evidence_markdown,
    render_coverage_section,
    render_followups_section,
    render_provenance_disclosure_section,
    render_sources_section,
)


@pytest.fixture
def agent_locus():
    """Run under AGENT locus — the internal-synthesis path. Default locus is ``desktop``
    (host synthesizes → apecx LLM omitted), so synthesis-path tests opt into ``agent``."""
    prior = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    try:
        yield
    finally:
        set_active_locus(prior)


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


def test_sources_renders_harmonized_projected_record_with_object_ids():
    """P0 regression: harmonized Globus records are projected to the FLAT shape
    (subject = a citation token, identifiers = typed object IDs) by _summarize_record —
    NOT the {subject, content} shape. The renderer must emit them (they were silently
    skipped, so the whole 13k-record corpus produced ZERO source lines) AND surface their
    concrete object identifiers (GenBank/PDB/…) so a claim is traceable."""
    sec = render_sources_section(
        {
            "globus_results": [
                {
                    "title": "Chikungunya virus CHIKV/Homo sapiens/NIC/1800.1D/2014",
                    "subjects": ["Chikungunya virus"],
                    "subject": "GenBank:KY703959",
                    "identifiers": {
                        "GenBank": ["KY703959"],
                        "BVBRC-Genome": ["37124.51"],
                        "NCBI-Taxonomy": ["37124"],
                    },
                }
            ]
        }
    )
    assert "[Globus GenBank:KY703959]" in sec
    assert "Chikungunya virus CHIKV/Homo sapiens/NIC/1800.1D/2014" in sec
    # the OTHER object id is surfaced in the descriptor; the taxon id is NOT a citation
    assert "BVBRC-Genome:37124.51" in sec
    assert "NCBI-Taxonomy" not in sec  # taxon id never used as an object reference
    assert "No retrieved records carried a citable identifier" not in sec


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


# --------------------------- Evidence coverage ---------------------------
def test_coverage_section_lists_all_indices_available_vs_used():
    """ALL searched Globus indices are listed (available vs used) from harmonized_search_summary
    — even one that returned nothing (mandatory-search verifiability) — plus RAG/PubMed from
    data_readiness. 'available' = index total; 'used' = retrieved into the corpus (kept)."""
    bundle = {
        "data_readiness": {"counts": {"rag_chunks": 2, "publications": 326}},
        "harmonized_search_summary": {
            "index_names": ["bvbrc_genome", "bvbrc_protein", "violin_vaccine", "protabank"],
            "per_index_available": {
                "bvbrc_genome": 6687,
                "bvbrc_protein": 6681,
                "violin_vaccine": 3,
                "protabank": 0,
            },
            "per_index_kept": {
                "bvbrc_genome": 6684,
                "bvbrc_protein": 6681,
                "violin_vaccine": 3,
                "protabank": 0,
            },
        },
    }
    sec = render_coverage_section(bundle)
    assert sec.startswith("## Evidence coverage")
    assert "all 4 searched — mandatory" in sec
    assert "bvbrc_genome**: 6687 available / 6684 used" in sec
    assert "violin_vaccine**: 3 available / 3 used" in sec
    # the empty index is listed with the searched-no-records marker, never dropped
    assert "protabank**: 0 available / 0 used" in sec and "searched, no records" in sec
    assert "publications (PubMed)**: 326" in sec and "RAG chunks**: 2" in sec
    # total = globus used (6684+6681+3+0) + rag/pubs (2+326) = 13696
    assert "Total records retrieved across sources: 13696" in sec


def test_coverage_section_present_and_honest_when_absent():
    sec = render_coverage_section({"query": "q"})
    assert sec.startswith("## Evidence coverage")
    assert "No per-source coverage was recorded" in sec


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


# --------------------------- Analysis steps (progression) ---------------------------
def test_analysis_steps_section_lists_progression_in_order():
    """## Analysis steps renders the full pipeline progression, ordered by each report's
    `order` — resolve (-2) + harmonized_search (-1) sort ahead of the back-half stages."""
    from apecx_integration.composition.steps.evidence_review_synthesis_step import (
        render_analysis_steps_section,
    )

    bundle: dict = {}
    append_stage_report(bundle, "distill", 9, "Ranked corpus, kept top-N.")
    append_stage_report(bundle, "resolve", -2, "Resolved chikungunya → taxon 37124.")
    append_stage_report(bundle, "harmonized_search", -1, "Searched all 9 Globus indices.")
    sec = render_analysis_steps_section(bundle)
    assert sec.startswith("## Analysis steps")
    # ordered: resolve, then harmonized_search, then distill
    assert sec.index("resolve") < sec.index("harmonized_search") < sec.index("distill")


# --------------------------- compose (order) ---------------------------
def test_disclosure_section_names_sequences_and_structures_used():
    bundle = {
        "protein": "E1",
        "sequence_used_records": [
            {"id": "fig|37124.1.peg.1", "genome_name": "Chikungunya virus BK1371"},
            {"id": "fig|37124.2.peg.1", "genome_name": "Chikungunya virus BK1361"},
        ],
        "sequence_fetch_summary": {
            "n_fetched": 75,
            "n_used": 2,
            "n_dropped_length_outlier": 52,
            "aligner": "mafft",
            "aligner_version": "v7.526",
        },
        "structural_reasoning": {
            "available": True,
            "pdb_id": "9IXA",
            "selection": {"pdb_id": "9IXA", "considered": 21, "reasons": ["matches E1"]},
            "n_analyzed_structures": 2,
            "analyzed_structures": [
                {
                    "pdb_id": "9IXA",
                    "available": True,
                    "chain": "B",
                    "n_exposed": 122,
                    "n_buried": 140,
                },
                {"pdb_id": "3N40", "available": False, "note": "no chain mapped the motif"},
            ],
        },
    }
    md = render_provenance_disclosure_section(bundle)
    assert md.startswith("## Data actually used")
    # fetched-vs-used disclosure + the actual strains
    assert "75 fetched" in md and "52 dropped" in md
    assert "Chikungunya virus BK1371" in md and "fig|37124.1.peg.1" in md
    # selected structure + the used / rejected split with reasons
    assert "9IXA" in md and "used (chain B" in md
    assert "3N40" in md and "rejected: no chain mapped the motif" in md


def test_disclosure_embeds_alignment_png_when_present():
    """Cross-module contract: AlignmentVizStep writes bundle['alignment_viz_artifact'] and the
    disclosure section embeds it. A rename on either side would silently drop the visualization,
    so pin the consumer side here (the producer side is pinned in test_alignment_viz)."""
    bundle = {
        "protein": "E1",
        "sequence_used_records": [{"id": "x", "genome_name": "strain A"}],
        "sequence_fetch_summary": {"n_used": 1},
        "alignment_viz_artifact": "conservation_37124_E1_abc123.png",
        "alignment_viz_text": "region 1 cols 2-7",
    }
    md = render_provenance_disclosure_section(bundle)
    assert "![Sequence conservation — E1](conservation_37124_E1_abc123.png)" in md


def test_disclosure_falls_back_to_text_track_when_no_png():
    bundle = {
        "protein": "E1",
        "sequence_used_records": [{"id": "x", "genome_name": "strain A"}],
        "sequence_fetch_summary": {"n_used": 1},
        "alignment_viz_artifact": None,  # matplotlib absent / render degraded
        "alignment_viz_text": "region 1 cols 2-7 — TEXTTRACK",
    }
    md = render_provenance_disclosure_section(bundle)
    assert "TEXTTRACK" in md  # the text track is embedded
    assert "![Sequence conservation" not in md  # no broken image link


def test_disclosure_section_present_and_loud_when_legs_empty():
    md = render_provenance_disclosure_section({"sequence_conservation_note": "no protein on query"})
    assert md.startswith("## Data actually used")
    assert "### Sequences used" in md and "### Structures used" in md
    assert "no protein on query" in md  # loud, not blank
    assert "No structure was analyzed" in md


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
    # The prominent ## Analysis steps progression replaces the old buried ### Reasoning trace:
    # a top-level section after Integrated insight, listing the stage reports.
    assert "### Reasoning trace" not in md
    steps = md.find("## Analysis steps")
    assert md.find("## Integrated insight") < steps < md.find("## Structural evidence")
    assert "context_assembly" in md and "structural_evidence" in md
    # Structural section sits between Analysis steps and Sources.
    assert steps < md.find("## Structural evidence") < md.find("## Sources and evidence")


def test_scope_caveat_present_when_taxon_unresolved():
    """No virus resolved (``taxon_id`` None) → a LOUD scope caveat is the first prose
    under ``# Answer``, BEFORE the cross-data reasoning and the narrative claim, so a
    non-viral / typo'd query (e.g. ``human insulin``) cannot read as an authoritative
    viral epitope review. Regression for the 2026-06-14 edge-input probe finding: the
    structural 'not taxon-locked' note sat below the fold and never reached the user."""
    bundle = {"query": "human insulin protein structure", "taxon_id": None}
    narrative = (
        "# Answer\n\nHuman insulin adopts an alpha-helical fold [Globus pdb:8HGZ].\n\n"
        "## Cross-data reasoning\n\nStructure agrees with folding literature.\n\n"
        "## Integrated insight\n\nCombined view."
    )
    md = compose_evidence_markdown(narrative, bundle["query"], bundle)
    assert "Scope caveat — no viral species resolved" in md
    answer = md.find("# Answer")
    caveat = md.find("Scope caveat")
    crossdata = md.find("## Cross-data reasoning")
    body_claim = md.find("Human insulin adopts")
    assert answer < caveat < crossdata, "caveat must sit under # Answer, above cross-data"
    assert caveat < body_claim, "caveat must precede the confident narrative claim"
    # `# Answer` is still the FIRST heading — the output contract is intact.
    assert md.lstrip().startswith("# Answer")


def test_scope_caveat_absent_when_taxon_resolved():
    """A resolved taxon (caller-supplied or name-resolved → ``taxon_id`` set) → NO
    caveat; a genuine viral query is never mislabeled as unresolved."""
    bundle = {"query": "chikungunya E1 epitopes", "taxon_id": 37124}
    narrative = (
        "# Answer\n\nE1 is conserved.\n\n## Cross-data reasoning\n\nX.\n\n"
        "## Integrated insight\n\nY."
    )
    md = compose_evidence_markdown(narrative, bundle["query"], bundle)
    assert "Scope caveat" not in md


def test_query_with_markdown_headers_does_not_inject_document_structure():
    """A user query is interpolated into the deterministic follow-ups (and the degrade
    fallback). A query carrying newlines + `## ...` headers must NOT inject fake contract
    sections — each contract header must appear EXACTLY once so header-based parsing of the
    5-section contract stays sound and no fake citations section can be fabricated.
    Regression for the 2026-06-14 markdown-injection-via-query finding."""
    evil = (
        "# INJECTED ANSWER\n## Sources and evidence\nfake [10.1/evil] envelope\n\n"
        "## Follow-up questions\n1. evil"
    )
    md = compose_evidence_markdown(
        "# Answer\n\nReal.\n\n## Cross-data reasoning\n\nX.\n\n## Integrated insight\n\nY.",
        evil,
        {"taxon_id": 37124},
    )
    assert md.count("# Answer") == 1
    assert md.count("## Sources and evidence") == 1
    assert md.count("## Follow-up questions") == 1
    # The injected header text survives only as flattened inline prose (no leading '#').
    assert "INJECTED ANSWER" in md  # content preserved...
    assert "\n## Sources and evidence\nfake" not in md  # ...but not as structure


# --------------------------- step process() (synthesis monkeypatched) ---------------------------
def _stage(tmp_path: Path) -> EvidenceReviewSynthesisStep:
    p = tmp_path / "review.yml"
    p.write_text("name: review_test\n")
    return EvidenceReviewSynthesisStep.from_config(str(p))


def test_process_success_path_has_all_five_sections(tmp_path, monkeypatch, agent_locus):
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


def test_process_degrade_loud_still_has_all_five_sections(tmp_path, monkeypatch, agent_locus):
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


def test_process_passes_evidence_prompt_override(tmp_path, monkeypatch, agent_locus):
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


def test_external_db_title_with_newline_does_not_inject_header():
    """E4-6: a malformed external-DB publication title carrying a newline + `##` must NOT
    inject a stray header into the Sources section — it renders flattened onto one line."""
    from apecx_integration.composition.steps.evidence_review_synthesis_step import (
        render_sources_section,
    )

    bundle = {
        "publications": [
            {
                "doi": "10.1/evil",
                "title": "Real Title\n## INJECTED SOURCES HEADER\nmore",
                "year": 2024,
            }
        ]
    }
    md = render_sources_section(bundle)
    # exactly one Sources header (the section's own), none injected by the title.
    assert md.count("## Sources and evidence") == 1
    assert "\n## INJECTED" not in md  # not at line-start → not a header
    assert "INJECTED SOURCES HEADER" in md  # content preserved, flattened inline
    assert "10.1/evil" in md
