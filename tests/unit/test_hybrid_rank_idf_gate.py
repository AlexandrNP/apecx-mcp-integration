"""Cycle-2 unit tests: IDF keyword weighting + adaptive relevance gate + tightened stem.

SYNTHETIC fixtures only — all records, questions and tokens are fabricated nonsense.
No real question, PMID, network, PubMed, or embedding model. The embedding tiebreak is
stubbed (``_embed_sims``) so the tests need no ``rag`` extra and stay deterministic.
These exercise the ranking MECHANISM only (domain-blind).

Covers the three cycle-2 levers:
  (a) IDF-weighted keyword signal   -> a rare facet outranks several common ones.
  (b) adaptive relevance gate       -> fewer than 15 on a sharp score drop; all-zero
                                       keyword scores fall back to embedding top-15.
  (c) tightened stem match          -> a 4-char shared prefix no longer over-matches.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.agents.literature.facets import (
    facets_present,
    keyword_facet_hits,
)


def _rag_step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    step = LiteratureRagStep.from_config(yml)
    step._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize tiebreak by default
    return step


class _CaptureLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):  # pragma: no cover - not asserted
        pass


def _rec(pmid: str, title: str = "", abstract: str = "") -> dict:
    return {"pmid": pmid, "title": title, "abstract": abstract}


# --- lever (a): IDF rarity weighting --------------------------------------


def test_one_rare_facet_outranks_several_common_facets():
    """A record matching ONE RARE facet ranks above one matching THREE COMMON facets.

    'alpha'/'bravo'/'charlie' saturate the candidate set (low IDF); 'zephyr' appears in a
    single record (high IDF). With equal-weight counting the 3-common record would win 3:1;
    IDF weighting flips it because the lone rare facet is far more discriminative.
    """
    step = _rag_step()
    question = "alpha bravo charlie zephyr"
    # 28 fillers + commonrec all carry the three COMMON facets; rarerec carries only the
    # single RARE facet. N=30 is large enough that idf(zephyr) > 3 * idf(common).
    records = [_rec("rarerec", title="zephyr"), _rec("commonrec", title="alpha bravo charlie")]
    records += [_rec(f"fill{i}", title="alpha bravo charlie") for i in range(28)]

    ranked = step._hybrid_rank(question, records, 15)
    pmids = [r["pmid"] for r in ranked]
    assert pmids[0] == "rarerec"  # rare facet wins the top slot despite fewer hits
    assert pmids.index("rarerec") < pmids.index("commonrec")


# --- lever (b): adaptive relevance gate -----------------------------------


def test_gate_returns_fewer_than_15_on_sharp_drop():
    """A sharp score drop -> the gate cuts the low-relevance tail; result is < 15, >= 3."""
    step = _rag_step()
    question = "zephyr quokka mimsy alpha"
    # 4 STRONG records carry the three rare facets (high IDF score); 20 WEAK records carry
    # only the common 'alpha' (low IDF). The gap between the two tiers is sharp.
    strong = [_rec(f"strong{i}", title="zephyr quokka mimsy") for i in range(4)]
    weak = [_rec(f"weak{i}", title="alpha") for i in range(20)]

    ranked = step._hybrid_rank(question, strong + weak, 15)
    pmids = {r["pmid"] for r in ranked}
    assert len(ranked) == 4  # only the strong tier survives the gate
    assert 3 <= len(ranked) <= 15
    assert len(ranked) < 24  # NOT the forced-15 / full set
    assert pmids == {f"strong{i}" for i in range(4)}


def test_gate_respects_min_floor_and_max_cap():
    step = _rag_step()
    # One strongly-relevant record (rare facet) among many irrelevant-but-usable ones.
    records = [_rec("hit", title="zephyr")]
    records += [_rec(f"noise{i}", title="unrelated filler words") for i in range(10)]
    ranked = step._hybrid_rank("zephyr quokka", records, 15)
    # Gate would keep only "hit", but the recall floor protects min(3, n_available)=3.
    assert len(ranked) == 3
    assert ranked[0]["pmid"] == "hit"


def test_all_zero_keyword_scores_fall_back_to_embedding_top_15():
    """No facet matches any candidate -> embedding-cosine top-15, loudly warned."""
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    records = [_rec(f"r{i}", title="unrelated filler words") for i in range(20)]
    # Descending cosine so r0 is most similar; fallback must return the top-15 by cosine.
    sims_by_pmid = {f"r{i}": float(20 - i) for i in range(20)}
    step._embed_sims = lambda q, recs: [sims_by_pmid[r["pmid"]] for r in recs]

    ranked = step._hybrid_rank("zephyr quokka", records, 15)
    assert [r["pmid"] for r in ranked] == [f"r{i}" for i in range(15)]
    assert len(ranked) == 15
    assert any("facet" in w.lower() and "embedding" in w.lower() for w in step.nb_logger.warnings)


# --- lever (c): tightened stem match --------------------------------------


def test_tightened_stem_rejects_4char_shared_prefix_keeps_5char_variant():
    # 4-char shared prefix "glim" no longer matches the 5-char stem "glimm" ...
    assert keyword_facet_hits(["glimmer"], "the glim creature") == 0
    assert facets_present(["glimmer"], "the glim creature") == []
    # ... but a genuine >=5-char morphological variant still matches.
    assert keyword_facet_hits(["glimmer"], "the glimmered light") == 1
    assert facets_present(["glimmer"], "the glimmered light") == ["glimmer"]


def test_four_char_facet_uses_whole_token_prefix():
    # A 4-char facet (shorter than the 5-char floor) matches via its WHOLE token as prefix.
    assert keyword_facet_hits(["blah"], "blahs here") == 1  # morphological variant
    assert keyword_facet_hits(["blah"], "bla here") == 0  # shorter word, no match


def test_short_token_still_whole_word_only():
    assert keyword_facet_hits(["xy"], "the xy marker") == 1
    assert keyword_facet_hits(["xy"], "the xyz marker") == 0


def test_facets_present_returns_distinct_in_facet_order():
    present = facets_present(["zephyr", "quokka", "mimsy"], "mimsy zephyr mimsy")
    assert present == ["zephyr", "mimsy"]  # distinct, ordered by the facet list
