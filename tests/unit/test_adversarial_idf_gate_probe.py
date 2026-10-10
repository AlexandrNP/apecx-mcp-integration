"""ADVERSARIAL probe of LiteratureRagStep._hybrid_rank (cycle-2 IDF + adaptive gate).

Author: adversarial test engineer, domain-BLIND. Every token/pmid/question below is
fabricated nonsense; no real question, PMID, network, PubMed, or embedding model is
touched. The embedding tiebreak (`_embed_sims`) is stubbed to known values so these
tests are deterministic and need no `rag` extra. MECHANISM only.

Independent fixtures (do NOT reuse the existing suites). Six required cases plus three
invariant-break probes that document where a stated guarantee does NOT hold.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.agents.literature.facets import (
    facets_present,
    keyword_facet_hits,
)


def _step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    s = LiteratureRagStep.from_config(yml)
    s._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize tiebreak unless overridden
    return s


def _rec(pmid, title="", abstract=""):
    return {"pmid": pmid, "title": title, "abstract": abstract}


class _CaptureLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        pass


# --- Case 1: IDF rarity beats raw count -----------------------------------


def test_case1_one_rare_facet_beats_three_common_facets():
    """df: 'quux' in 1/30 records (rare), 'foo/baz/qux' in 29/30 (common).

    idf(rare)=3.741 > 3*idf(common)=3.098, so the single-rare record outranks the
    three-common record despite 1 hit vs 3.
    """
    step = _step()
    q = "foo baz qux quux"
    recs = [_rec("rarehit", title="quux"), _rec("commonhit", title="foo baz qux")]
    recs += [_rec(f"f{i}", title="foo baz qux") for i in range(28)]  # N=30
    ranked = step._hybrid_rank(q, recs, 15)
    pmids = [r["pmid"] for r in ranked]
    assert pmids[0] == "rarehit"
    assert pmids.index("rarehit") < pmids.index("commonhit")


# --- Case 2: adaptive gate shrinks output (sharp drop) + caps (flat) ------


def test_case2a_sharp_drop_returns_fewer_than_15():
    """Two strong records (2 rare facets) + 20 weak (1 common) -> gate keeps only strong."""
    step = _step()
    q = "quux grux flux foo"
    strong = [_rec(f"s{i}", title="quux grux flux") for i in range(2)]
    weak = [_rec(f"w{i}", title="foo") for i in range(20)]
    ranked = step._hybrid_rank(q, strong + weak, 15)
    # 2 strong < floor(3) -> recall floor pulls in the next-best (one weak). Result 3, not 15.
    assert len(ranked) == 3
    assert {"s0", "s1"} <= {r["pmid"] for r in ranked}


def test_case2b_flat_high_distribution_caps_at_15():
    """40 records all carrying the same 3 facets -> flat scores -> capped at exactly 15."""
    step = _step()
    recs = [_rec(f"r{i}", title="quux grux flux") for i in range(40)]
    ranked = step._hybrid_rank("quux grux flux", recs, 15)
    assert len(ranked) == 15


# --- Case 3: recall floor protects min(3, n) ------------------------------


def test_case3_only_one_clears_threshold_floor_keeps_three():
    step = _step()
    recs = [_rec("top", title="quux grux flux")]  # 3 rare facets, highest score
    recs += [_rec(f"n{i}", title="foo") for i in range(9)]  # 1 common facet, below 0.4*top
    ranked = step._hybrid_rank("quux grux flux foo", recs, 15)
    assert len(ranked) == 3  # floor = min(3,10)
    assert ranked[0]["pmid"] == "top"


def test_case3_floor_caps_at_availability_when_n_below_three():
    step = _step()
    recs = [_rec("top", title="quux"), _rec("low", title="foo")]
    ranked = step._hybrid_rank("quux foo", recs, 15)
    assert len(ranked) == 2  # min(3, n=2); never a crash, never 3-from-2


# --- Case 4: all-zero keyword fallback to embedding top-15 -----------------


def test_case4_all_zero_falls_back_to_embedding_top15():
    step = _step()
    step.nb_logger = _CaptureLogger()
    recs = [_rec(f"r{i}", title="unrelated filler tokens") for i in range(20)]
    sims = {f"r{i}": float(20 - i) for i in range(20)}  # r0 most similar
    step._embed_sims = lambda q, rs: [sims[r["pmid"]] for r in rs]
    ranked = step._hybrid_rank("quux grux flux", recs, 15)  # no facet matches
    assert [r["pmid"] for r in ranked] == [f"r{i}" for i in range(15)]
    assert len(ranked) == 15
    assert ranked  # never empty
    assert len(ranked) < 20  # not the full set (n > 15 here)
    assert any("facet" in w.lower() for w in step.nb_logger.warnings)


# --- Case 5: cap still exactly 15 -----------------------------------------


def test_case5_more_than_15_above_threshold_caps_at_15():
    step = _step()
    recs = [_rec(f"r{i}", title="quux grux flux") for i in range(25)]
    ranked = step._hybrid_rank("quux grux flux", recs, 15)
    assert len(ranked) == 15


# --- Case 6: tightened stem (>=5 stem; <=3 whole-word) --------------------


def test_case6_four_char_shared_prefix_no_hit():
    assert keyword_facet_hits(["frobnicate"], "the frob gadget") == 0
    assert facets_present(["frobnicate"], "the frob gadget") == []


def test_case6_five_char_stem_variant_hits():
    assert keyword_facet_hits(["frobnicate"], "the frobnicated gadget") == 1
    assert facets_present(["frobnicate"], "the frobnicated gadget") == ["frobnicate"]


def test_case6_three_char_token_whole_word_only():
    assert keyword_facet_hits(["zap"], "the zap unit") == 1
    assert keyword_facet_hits(["zap"], "the zappy unit") == 0  # substring, no hit
    assert keyword_facet_hits(["zap"], "the bzap unit") == 0  # suffix, no hit


# --- Invariant-break probes (document where a stated guarantee is FALSE) ---


def test_break_fallback_never_full_set_FAILS_when_n_le_15():
    """PROBE: the fallback's 'never the full set' claim breaks for n<=15.

    With all-zero keyword scores and only 8 candidates, the embedding fallback returns
    emb_order[:15] = ALL 8 (merely reordered). So 'never the full set' holds ONLY when
    n>15; at n<=15 the fallback IS the full set.
    """
    step = _step()
    step.nb_logger = _CaptureLogger()
    recs = [_rec(f"r{i}", title="unrelated filler tokens") for i in range(8)]
    step._embed_sims = lambda q, rs: [float(i) for i, _ in enumerate(rs)]
    ranked = step._hybrid_rank("quux grux", recs, 15)  # no facet matches
    assert len(ranked) == 8  # == full candidate set (reordered), NOT a subset


def test_break_recall_floor_violated_when_k_below_min_keep():
    """PROBE: the min(3,n) recall floor is NOT honored when caller passes k<3.

    Production always calls with _TOP_K=15, but _hybrid_rank applies kept[:k] AFTER the
    floor, so k=1 returns a single record even though floor=min(3,n)=3.
    """
    step = _step()
    recs = [_rec("top", title="quux grux flux")]
    recs += [_rec(f"n{i}", title="foo") for i in range(5)]
    ranked = step._hybrid_rank("quux grux flux foo", recs, 1)
    assert len(ranked) == 1  # floor of 3 silently defeated by k=1


def test_break_universal_facet_still_scores_one_not_zero():
    """PROBE: a facet present in EVERY candidate has idf=log(1)+1=1.0, not 0.

    So a 'useless' universal facet still contributes to rank — IDF here never zeroes a
    term, it only down-weights it. Raw count re-dominates when all facets share df.
    """
    step = _step()
    # Both records carry the universal 'foo'; 'rb' additionally carries one more universal.
    recs = [_rec("ra", title="foo"), _rec("rb", title="foo bar")]
    # make 'foo' and 'bar' both universal-ish by cloning; here df(foo)=2, df(bar)=1 of N=2
    ranked = step._hybrid_rank("foo bar", recs, 15)
    # rb (2 facets) outranks ra (1 facet): count re-dominates since idf gap is small.
    assert ranked[0]["pmid"] == "rb"
    assert len(ranked) == 2  # min(3,2) floor
