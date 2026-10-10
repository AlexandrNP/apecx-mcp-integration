"""Unit tests for the LiteratureRagStep hybrid rank+cap (SYNTHETIC fixtures only).

These exercise the ranking MECHANISM with fabricated records and nonsense tokens —
no real question, no real PMID, no network, no PubMed. The embedding tiebreak is
stubbed (``_embed_sims``) or forced to fail (``_get_embed_model``) so the tests
need no ``rag`` extra and stay deterministic.

The reader is the SINGLE rank+cap authority (K=15), applied in EVERY locus. The
real-data parity partner is tests/integration/test_literature_rag_tool_live.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.agents.literature.facets import keyword_facet_hits
from apecx_integration.composition.runtime.execution_locus import ExecutionLocus


def _step_yaml(name: str) -> str:
    import apecx_integration.composition.workflows.literature_rag.steps as pkg

    return str(Path(pkg.__file__).resolve().parent / name)


def _rag_step():
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    return LiteratureRagStep.from_config(_step_yaml("literature_rag_step.yml"))


class _CaptureLogger:
    """Minimal nb_logger stand-in that records warning/info messages."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):  # pragma: no cover - not asserted
        pass


def _rec(pmid: str, title: str = "", abstract: str = "") -> dict:
    return {"pmid": pmid, "title": title, "abstract": abstract}


# --- the shared keyword helper: stem + short-token rules -------------------


def test_keyword_stem_matches_morphological_variant():
    # token longer than 3 chars matches via a short stem-prefix
    assert keyword_facet_hits(["epitopes"], "the epitope was mapped") == 1
    assert keyword_facet_hits(["alpha"], "alphas and more") == 1


def test_keyword_short_token_matches_whole_word_only():
    # <=3-char token hits a whole word ...
    assert keyword_facet_hits(["ab2"], "the ab2 marker") == 1
    # ... but NEVER as a substring inside a longer word
    assert keyword_facet_hits(["ab2"], "the ab2x marker") == 0


def test_keyword_hits_count_is_distinct_facets():
    hits = keyword_facet_hits(["alpha", "beta", "gamma"], "alpha and beta only")
    assert hits == 2


# --- the hybrid rank: primary key, tiebreak, cap --------------------------


def test_more_keyword_hits_ranks_above_fewer():
    step = _rag_step()
    step._embed_sims = lambda q, records: [0.0] * len(records)  # neutralize tiebreak
    records = [
        _rec("few", title="alpha only"),  # 1 hit
        _rec("many", title="alpha beta gamma together"),  # 3 hits
    ]
    ranked = step._hybrid_rank("alpha beta gamma", records, 15)
    assert [r["pmid"] for r in ranked] == ["many", "few"]


def test_ties_on_keyword_hits_broken_by_embedding_order():
    step = _rag_step()
    # All three have the SAME single keyword hit -> embedding cosine decides.
    records = [
        _rec("low", title="alpha"),
        _rec("high", title="alpha"),
        _rec("mid", title="alpha"),
    ]
    sims_by_pmid = {"low": 0.1, "high": 0.9, "mid": 0.5}
    step._embed_sims = lambda q, recs: [sims_by_pmid[r["pmid"]] for r in recs]
    ranked = step._hybrid_rank("alpha", records, 15)
    assert [r["pmid"] for r in ranked] == ["high", "mid", "low"]


def test_hybrid_rank_drops_records_with_no_usable_text():
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    records = [_rec("empty"), _rec("good", title="alpha")]
    ranked = step._hybrid_rank("alpha", records, 15)
    assert [r["pmid"] for r in ranked] == ["good"]


def test_desktop_locus_caps_at_15(monkeypatch):
    monkeypatch.setattr(
        "apecx_integration.composition.runtime.execution_locus.get_active_locus",
        lambda: ExecutionLocus.DESKTOP,
    )
    step = _rag_step()
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    records = [_rec(f"pmid{i}", title=f"alpha record {i}") for i in range(180)]
    out = asyncio.run(step.process({"question": "alpha beta gamma", "filtered_records": records}))
    assert out["status"] == "host_synthesizes"
    assert len(out["citations"]) == 15  # NOT the full 180


def test_embedding_unavailable_keyword_only_fallback_still_caps():
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    step._get_embed_model = lambda: (_ for _ in ()).throw(RuntimeError("no rag extra"))
    # 20 records; only "hit" mentions the facet, so it alone clears the relevance gate.
    records = [_rec(f"pmid{i}", title="noise text") for i in range(19)]
    records.append(_rec("hit", title="alpha beta gamma"))
    ranked = step._hybrid_rank("alpha beta gamma", records, 15)
    # IDF-keyword-only ranking still runs; the gate cuts the zero-score tail down to the
    # recall floor (3), capped, NEVER the full 20.
    assert len(ranked) == 3
    assert len(ranked) < len(records)
    assert ranked[0]["pmid"] == "hit"  # keyword signal still ranks it first
    assert any("keyword" in w.lower() for w in step.nb_logger.warnings)
