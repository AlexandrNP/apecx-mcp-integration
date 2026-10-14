"""ADVERSARIAL independent fixtures for the LiteratureRagStep hybrid rank+cap.

Author: adversarial test engineer (domain-blind). All strings fabricated nonsense;
no real question, PMID, network, or embedding model. Tiebreak (``_embed_sims``) is
stubbed to known values; the backend LLM path is fully stubbed. Verifies the
ranking MECHANISM only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.agents.literature.facets import (
    extract_question_facets,
    keyword_facet_hits,
)
from apecx_integration.composition.runtime.execution_locus import ExecutionLocus

RAG_STEP_MOD = "apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step"
LOCUS_FN = "apecx_integration.composition.runtime.execution_locus.get_active_locus"


def _step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    s = LiteratureRagStep.from_config(yml)
    s._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize tiebreak by default
    return s


def _rec(pmid, title="", abstract=""):
    return {"pmid": pmid, "title": title, "abstract": abstract}


def _desktop(mp):
    mp.setattr(LOCUS_FN, lambda: ExecutionLocus.DESKTOP)


def _agent(mp):
    mp.setattr(LOCUS_FN, lambda: ExecutionLocus.AGENT)


def _stub_backend_llm(mp):
    """Stub the whole backend synthesis path; capture chunk count reaching synth."""
    captured = {}

    def fake_synth(question, rag_chunks, llm, config):
        captured["n"] = len(rag_chunks)
        return " ".join(f"[RAG chunk #{i}]" for i in range(1, len(rag_chunks) + 1))

    mp.setattr(f"{RAG_STEP_MOD}.synthesize_response", fake_synth)
    mp.setattr("apecx_integration.agents._llm_config.preflight_llm_model", lambda: None)
    mp.setattr("apecx_integration.agents._llm_factory.build_chat_llm", lambda: object())
    return captured


# --- Case 1: hard cap = 35 -------------------------------------------------


def test_desktop_hard_cap_exactly_35(monkeypatch):
    _desktop(monkeypatch)
    step = _step()
    recs = [_rec(f"zq{i}", title="wobble gribble snorf") for i in range(200)]
    out = asyncio.run(step.process({"question": "wobble gribble snorf", "filtered_records": recs}))
    assert out["status"] == "host_synthesizes"
    assert len(out["citations"]) == 35


def test_agent_hard_cap_exactly_35(monkeypatch):
    _agent(monkeypatch)
    cap = _stub_backend_llm(monkeypatch)
    step = _step()
    recs = [_rec(f"zq{i}", title="wobble gribble snorf") for i in range(200)]
    out = asyncio.run(step.process({"question": "wobble gribble snorf", "filtered_records": recs}))
    assert out["status"] == "ok"
    assert cap["n"] == 35  # exactly 35 chunks reached the synthesizer
    assert len(out["citations"]) == 35


def test_fewer_than_cap_returns_all_no_padding(monkeypatch):
    _desktop(monkeypatch)
    step = _step()
    recs = [_rec(f"zq{i}", title="wobble gribble snorf") for i in range(5)]
    out = asyncio.run(step.process({"question": "wobble gribble snorf", "filtered_records": recs}))
    assert out["status"] == "host_synthesizes"
    assert len(out["citations"]) == 5


def test_hybrid_rank_direct_cap(monkeypatch):
    step = _step()
    recs = [_rec(f"zq{i}", title="wobble") for i in range(200)]
    ranked = step._hybrid_rank("wobble", recs, 15)
    assert len(ranked) == 15


# --- Case 2: primary key then tiebreak -------------------------------------


def test_keyword_count_dominates_cosine(monkeypatch):
    step = _step()
    # "few" has HIGH cosine but 1 hit; "many" has LOW cosine but 3 hits.
    step._embed_sims = lambda q, recs: [0.99 if r["pmid"] == "few" else 0.0 for r in recs]
    recs = [
        _rec("few", title="wobble"),
        _rec("many", title="wobble gribble snorf"),
    ]
    ranked = step._hybrid_rank("wobble gribble snorf", recs, 15)
    assert [r["pmid"] for r in ranked] == ["many", "few"]


def test_cosine_breaks_equal_keyword_ties(monkeypatch):
    step = _step()
    sims = {"a": 0.2, "b": 0.8, "c": 0.5}
    step._embed_sims = lambda q, recs: [sims[r["pmid"]] for r in recs]
    recs = [_rec("a", title="wobble"), _rec("b", title="wobble"), _rec("c", title="wobble")]
    ranked = step._hybrid_rank("wobble", recs, 15)
    assert [r["pmid"] for r in ranked] == ["b", "c", "a"]


# --- Case 3: empty facets --------------------------------------------------


def test_only_stopwords_yields_empty_facets():
    assert extract_question_facets("the of and to in on with") == []


def test_empty_facets_still_caps_and_no_crash(monkeypatch):
    _desktop(monkeypatch)
    step = _step()
    recs = [_rec(f"zq{i}", title="wobble gribble snorf") for i in range(200)]
    out = asyncio.run(step.process({"question": "the of and to in", "filtered_records": recs}))
    assert out["status"] == "host_synthesizes"
    assert len(out["citations"]) == 35  # ranked+capped, never the full 200


# --- Case 4: no usable text ------------------------------------------------


def test_all_empty_text_no_evidence(monkeypatch):
    _desktop(monkeypatch)
    step = _step()
    recs = [_rec(f"zq{i}", title="", abstract="") for i in range(10)]
    out = asyncio.run(step.process({"question": "wobble gribble", "filtered_records": recs}))
    assert out["status"] == "no_evidence"
    assert out["answer"] == ""
    assert out["citations"] == []


# --- Case 5: stem-rule edges (nonsense tokens isolate the rule) ------------


def test_long_token_matches_morphological_variant_via_stem():
    # facet >=5 chars -> 5-char stem "blorp" matches the morphological variant "blorped"
    assert keyword_facet_hits(["blorping"], "the blorped widget") == 1
    # the 4-char prefix "blor" is now too short for the 5-char stem -> no hit (lever c)
    assert keyword_facet_hits(["blorping"], "blor matched too") == 0


def test_short_token_whole_word_only_never_substring():
    assert keyword_facet_hits(["xqz"], "the xqz marker") == 1  # whole word hit
    assert keyword_facet_hits(["xqz"], "the xqzab marker") == 0  # longer word, no hit
    assert keyword_facet_hits(["xqz"], "the axqz marker") == 0  # suffix, no hit


def test_long_token_stem_no_longer_overmatches_short_shared_prefix():
    # Cycle-2 lever c: "wobbling" now stems to the 5-char "wobbl"; an UNRELATED word
    # sharing only the 4-char prefix ("wobbat") no longer counts as a hit ...
    assert keyword_facet_hits(["wobbling"], "the wobbat creature") == 0
    # ... while a genuine >=5-char stem variant ("wobbled") still does.
    assert keyword_facet_hits(["wobbling"], "the wobbled creature") == 1


# --- Case 6: stability / dedup ---------------------------------------------


def test_duplicate_records_deterministic_no_crash():
    step = _step()
    step._embed_sims = lambda q, recs: [0.5] * len(recs)  # identical scores
    recs = [_rec("dup", title="wobble gribble") for _ in range(6)]
    r1 = step._hybrid_rank("wobble gribble", recs, 15)
    r2 = step._hybrid_rank("wobble gribble", recs, 15)
    assert [r["pmid"] for r in r1] == [r["pmid"] for r in r2]
    assert len(r1) == 6


def test_identical_scores_preserve_input_order():
    step = _step()
    step._embed_sims = lambda q, recs: [0.5] * len(recs)
    recs = [_rec("a", title="wobble"), _rec("b", title="wobble"), _rec("c", title="wobble")]
    ranked = step._hybrid_rank("wobble", recs, 15)
    assert [r["pmid"] for r in ranked] == ["a", "b", "c"]  # stable sort
