"""Unit tests for the ``literature_qa`` evidence block — citation + chunk + score formatting.

PURE / SYNTHETIC fixtures only: fabricated bibliographic fields, fabricated chunk text, and
INJECTED retrieval scores. No network, no LLM, no real model, no real content. These exercise
the PRESENTATION layer added on the cycle-15 base:

  1. :func:`build_evidence_blocks` — records + injected per-document scores -> ordered blocks.
  2. :func:`render_evidence_markdown` — blocks -> deterministic Markdown.
  3. The step ``process`` desktop (``host_synthesizes``) and agent (``ok``) paths populate
     ``result["evidence"]`` — the two execution loci the brief requires.
  4. INVARIANCE: threading the scores side-channel does NOT change the ranked document
     set/order (``_hybrid_rank`` returns the same records with and without ``return_scores``).
  5. The ``literature_qa`` tool surfaces the reader's ``evidence`` pass-through (and omits it
     when the reader produced none, leaving the pre-existing degrade shape byte-identical).

Mirrors the fixture style of ``test_literature_rag_hybrid_rank.py`` /
``test_literature_rag_step_smoke.py``. The embedding tiebreak is stubbed to zeros so no
optional ``rag`` extra is needed; the import-skip guard below is defensive only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Defensive clean-install guard: the module under test imports NO optional extra at module
# scope (heavy imports are lazy inside methods), so this importorskip never actually skips on a
# base install — it is here per the brief and would only trigger if the module surface changed.
pytest.importorskip(
    "apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step"
)

from apecx_integration.composition.runtime.execution_locus import ExecutionLocus  # noqa: E402
from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (  # noqa: E402
    LiteratureRagStep,
    build_evidence_blocks,
    render_evidence_markdown,
)


def _step_yaml() -> str:
    import apecx_integration.composition.workflows.literature_rag.steps as pkg

    return str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")


def _rag_step() -> LiteratureRagStep:
    return LiteratureRagStep.from_config(_step_yaml())


def _rec(pmid: str = "", title: str = "", abstract: str = "", **extra) -> dict:
    rec = {"title": title, "abstract": abstract}
    if pmid:
        rec["pmid"] = pmid
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
# 1. build_evidence_blocks — pure, injected scores + fabricated fields          #
# --------------------------------------------------------------------------- #
def test_build_evidence_blocks_shape_citation_chunks_scores():
    records = [
        {"pmid": "P1", "pmcid": "PMC1", "title": "Title one", "abstract": "Body one tokens"},
        {"pmid": "P2", "title": "Title two", "abstract": "Body two tokens"},  # no pmcid
        {"title": "Title three", "abstract": "Body three"},  # no pmid, no pmcid
    ]
    scores = [
        {
            "keyword_idf": 2.5,
            "semantic_cosine": 0.9,
            "discriminative_coverage": 2.0,
            "cross_encoder": 0.8,
        },
        {"keyword_idf": 1.0, "semantic_cosine": 0.4},
        {"keyword_idf": 0.5},
    ]

    blocks = build_evidence_blocks(records, scores, char_cap=12_000)

    # One block per returned document, in the returned order, rank 1..N.
    assert len(blocks) == 3
    assert [b["rank"] for b in blocks] == [1, 2, 3]

    # Citation echoes ONLY present fields, OMITS absent ones (never null-filled).
    assert blocks[0]["citation"] == {"pmid": "P1", "pmcid": "PMC1", "title": "Title one"}
    assert blocks[1]["citation"] == {"pmid": "P2", "title": "Title two"}  # pmcid omitted
    assert blocks[2]["citation"] == {"title": "Title three"}  # pmid + pmcid omitted

    # Chunks carry the fabricated text (title + grounding passage).
    assert blocks[0]["chunks"] == [{"text": "Title one\nBody one tokens"}]
    assert blocks[1]["chunks"] == [{"text": "Title two\nBody two tokens"}]

    # Scores echo the injected values verbatim.
    assert blocks[0]["scores"] == scores[0]
    assert blocks[1]["scores"] == scores[1]
    assert blocks[2]["scores"] == scores[2]


def test_build_evidence_blocks_uses_full_text_when_present_capped():
    rec = {"pmid": "P9", "title": "T", "abstract": "ABSTRACT", "full_text": "X" * 50}
    blocks = build_evidence_blocks([rec], [{"keyword_idf": 1.0}], char_cap=10)
    # full_text is preferred over abstract and truncated to char_cap; title is prepended.
    assert blocks[0]["chunks"] == [{"text": "T\n" + "X" * 10}]


# --------------------------------------------------------------------------- #
# 2/3. render_evidence_markdown — deterministic, contains citation + chunk text #
# --------------------------------------------------------------------------- #
def test_render_markdown_contains_citation_and_chunk_text_deterministic():
    records = [
        {"pmid": "P1", "pmcid": "PMC1", "title": "Alpha title", "abstract": "alpha body"},
        {"title": "Beta title", "abstract": "beta body"},  # no ids
    ]
    scores = [{"keyword_idf": 2.0, "semantic_cosine": 0.75}, {"keyword_idf": 0.3}]
    blocks = build_evidence_blocks(records, scores, char_cap=12_000)

    md = render_evidence_markdown(blocks)
    md_again = render_evidence_markdown(blocks)

    # Deterministic: pure function of the structure.
    assert md == md_again

    # Each document's present citation values + chunk text appear.
    assert "P1" in md and "PMC1" in md and "Alpha title" in md and "alpha body" in md
    assert "Beta title" in md and "beta body" in md
    # Scores rendered (sorted keys, fixed format).
    assert "keyword_idf=2.0000" in md and "semantic_cosine=0.7500" in md


def test_render_markdown_empty_evidence_is_empty_string():
    assert render_evidence_markdown([]) == ""


# --------------------------------------------------------------------------- #
# 4. INVARIANCE — scores side-channel does not change the ranked set/order      #
# --------------------------------------------------------------------------- #
def test_return_scores_does_not_change_ranked_set_or_order():
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize tiebreak, no rag extra
    records = [
        _rec("a", title="alpha beta"),
        _rec("b", title="alpha"),
        _rec("c", title="gamma delta alpha beta"),
        _rec("d", title="unrelated noise"),
    ]
    question = "alpha beta gamma delta"

    plain = step._hybrid_rank(question, records, 15)
    ranked, scores = step._hybrid_rank(question, records, 15, return_scores=True)

    # Document id-list + order IDENTICAL with and without the scores side-channel.
    assert [r["pmid"] for r in plain] == [r["pmid"] for r in ranked]
    # Scores aligned 1:1 with the ranked records; always carry the keyword signal (+ the stubbed
    # cosine since embeddings were "available"); no cross_encoder (CE stage off by default).
    assert len(scores) == len(ranked)
    for s in scores:
        assert "keyword_idf" in s and "semantic_cosine" in s
        assert "cross_encoder" not in s


def test_return_scores_empty_when_no_usable_records():
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    ranked, scores = step._hybrid_rank("alpha", [_rec("x"), _rec("y")], 15, return_scores=True)
    assert ranked == [] and scores == []


# --------------------------------------------------------------------------- #
# 5. process() populates evidence in BOTH loci (desktop + agent)                #
# --------------------------------------------------------------------------- #
def test_process_desktop_locus_populates_evidence(monkeypatch):
    monkeypatch.setattr(
        "apecx_integration.composition.runtime.execution_locus.get_active_locus",
        lambda: ExecutionLocus.DESKTOP,
    )
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    records = [
        _rec("m1", title="alpha beta", abstract="body one", pmcid="PMCm1"),
        _rec("m2", title="alpha", abstract="body two"),
    ]
    out = asyncio.run(step.process({"question": "alpha beta", "filtered_records": records}))

    assert out["status"] == "host_synthesizes"
    ev = out["evidence"]
    # One block per returned document; the block PMID set matches the returned citations.
    assert [b["rank"] for b in ev] == list(range(1, len(ev) + 1))
    assert {b["citation"].get("pmid") for b in ev} == set(out["citations"])
    # The matching-record block carries its present fields + the fabricated chunk text + scores.
    by_pmid = {b["citation"].get("pmid"): b for b in ev}
    assert by_pmid["m1"]["citation"] == {"pmid": "m1", "pmcid": "PMCm1", "title": "alpha beta"}
    assert by_pmid["m1"]["chunks"][0]["text"] == "alpha beta\nbody one"
    assert "keyword_idf" in by_pmid["m1"]["scores"]


def test_process_agent_ok_path_populates_evidence(monkeypatch):
    """Agent locus ok path: evidence populated. The LLM synthesis is stubbed (wiring smoke;
    the real-LLM parity partner is the Ollama-gated step/tool integration tests)."""
    monkeypatch.setattr(
        "apecx_integration.composition.runtime.execution_locus.get_active_locus",
        lambda: ExecutionLocus.AGENT,
    )
    monkeypatch.setattr(
        "apecx_integration.agents._llm_config.preflight_llm_model", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "apecx_integration.agents._llm_factory.build_chat_llm", lambda *a, **k: object()
    )
    import apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step as lrs

    monkeypatch.setattr(
        lrs, "synthesize_response", lambda q, **k: "Alpha is relevant. [RAG chunk #1]"
    )
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    records = [_rec("g1", title="alpha beta", abstract="body")]
    out = asyncio.run(step.process({"question": "alpha beta", "filtered_records": records}))

    assert out["status"] == "ok"
    assert out["evidence"][0]["citation"] == {"pmid": "g1", "title": "alpha beta"}
    assert out["evidence"][0]["chunks"][0]["text"] == "alpha beta\nbody"
    assert "keyword_idf" in out["evidence"][0]["scores"]


# --------------------------------------------------------------------------- #
# 6. Tool pass-through                                                          #
# --------------------------------------------------------------------------- #
_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_1"


def _wire_tool(monkeypatch, reader_result):
    from apecx_integration.mcp_surface.tools import literature_qa as tool

    stamped = [{"pmid": "111", "title": "t", "abstract": "a", "iris": [_IRI]}]
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(stamped))

    async def _fake_reader(question, filtered_records):
        return reader_result

    monkeypatch.setattr(tool, "_read_filtered", _fake_reader)
    return tool


def test_tool_surfaces_reader_evidence(monkeypatch):
    evidence = [
        {
            "rank": 1,
            "citation": {"pmid": "111"},
            "chunks": [{"text": "t\na"}],
            "scores": {"keyword_idf": 1.0},
        }
    ]
    tool = _wire_tool(
        monkeypatch,
        {"answer": "ok [PMID:111]", "citations": ["111"], "status": "ok", "evidence": evidence},
    )
    result = asyncio.run(tool.literature_qa("Org", "q?", 8))
    assert result["evidence"] == evidence
    assert result["status"] == "ok"


def test_tool_omits_evidence_when_reader_has_none(monkeypatch):
    tool = _wire_tool(
        monkeypatch, {"answer": "ok [PMID:111]", "citations": ["111"], "status": "ok"}
    )
    result = asyncio.run(tool.literature_qa("Org", "q?", 8))
    # No reader evidence -> the key is absent (pre-existing ok-shape stays byte-identical).
    assert "evidence" not in result
