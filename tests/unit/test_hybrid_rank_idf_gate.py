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


def _assert_rare_match_protected(m: int):
    """Invariant 1 (RARE-MATCH PROTECTION) for a question with 1 rare + ``m`` common facets.

    Tester A's counterexample: TOP records carry all facets, RAREONLY carries only the single
    highest-IDF facet, and a large COMMONONLY pile carries only the ``m`` common facets.
    RAREONLY must SURVIVE the gate; the COMMONONLY pile must be trimmed.

    This is exactly where the rejected fraction-of-SUM gate failed: TOP's SUM dwarfs
    RAREONLY's lone rare-facet SUM, so a high fraction of TOP's score evicts RAREONLY while
    COMMONONLY (SUM of m common hits) clears it — backwards. The discriminative-COVERAGE gate
    only counts DISCRIMINATIVE facets (the common facets fall below the rarity cutoff), so
    RAREONLY (coverage == TOP's — both cover the one rare facet) always survives while the
    COMMONONLY pile (coverage 0 — it matches no discriminative facet) is trimmed no matter
    how many commons it stacks.
    """
    common = ["alpha", "bravo", "charlie"][:m]
    question = "zephyr " + " ".join(common)
    all_facets_title = question
    # 2 TOP + 1 RAREONLY give the gate 3 discriminative survivors, so the recall floor
    # (min(_GATE_MIN_KEEP=3, n)) is met WITHOUT padding any common-only record back in.
    top = [_rec(f"top{i}", title=all_facets_title) for i in range(2)]
    rareonly = [_rec("rareonly", title="zephyr")]
    # 30 commons drive idf(common) toward its 1.0 floor and idf(zephyr) up -> a sharp peak gap.
    commononly = [_rec(f"common{i}", title=" ".join(common)) for i in range(30)]

    ranked = _rag_step()._hybrid_rank(question, top + rareonly + commononly, 15)
    pmids = {r["pmid"] for r in ranked}
    assert "rareonly" in pmids  # the lone rare-facet match is NOT gated out
    assert not any(p.startswith("common") for p in pmids)  # common-only pile trimmed, not padded
    assert pmids == {"top0", "top1", "rareonly"}


def test_rare_match_protected_with_two_common_facets():
    _assert_rare_match_protected(m=2)


def test_rare_match_protected_with_three_common_facets():
    _assert_rare_match_protected(m=3)


def test_common_pile_trimmed_despite_high_sum_regardless_of_stack_depth():
    """Invariant 2 (COMMON-PILE TRIMMING): a record matching ONLY common facets is trimmed
    even when its SUM is large, and stacking MORE commons never rescues it.

    Question = 1 rare ('zephyr') + 3 common. The off-topic pile matches all 3 commons (a big
    SUM of 3.0) but no rare facet; the common facets fall below the discriminative cutoff, so
    the pile's COVERAGE is 0 however many commons it stacks, while the on-target tier (covers
    the rare facet) survives.
    """
    question = "zephyr alpha bravo charlie"
    on_target = [_rec(f"on{i}", title="zephyr alpha bravo charlie") for i in range(3)]
    # Every pile record stacks all 3 common facets -> maximal common SUM, zero rare content.
    pile = [_rec(f"pile{i}", title="alpha bravo charlie") for i in range(20)]

    ranked = _rag_step()._hybrid_rank(question, on_target + pile, 15)
    pmids = {r["pmid"] for r in ranked}
    assert len(ranked) == 3  # only the on-target tier survives — NOT a padded 15
    assert pmids == {f"on{i}" for i in range(3)}
    assert not any(p.startswith("pile") for p in pmids)  # common-only pile dropped despite high SUM


def test_coverage_gate_trims_single_facet_records_when_richer_coverage_exists():
    """Invariant 3 (ANTI-UNDER-TRIM): with M near-equally-rare facets, records covering
    SEVERAL of them rank above and TRIM the many single-facet matches; the gate returns FEWER
    than min(N, k), never a padded k.

    The rejected peak-only gate returned the full k here: every record matching any ONE of the
    M equally-rare facets had disc_peak == best_peak, so none were cut. The discriminative-
    COVERAGE gate cuts the single-facet records because each covers only ~1/M of the
    discriminative mass the multi-facet 'rich' records cover.
    """
    question = "zephyr quokka mimsy"  # M=3 facets, driven near-equally rare below
    rich = [_rec(f"rich{i}", title="zephyr quokka mimsy") for i in range(5)]
    singles = (
        [_rec(f"zeph{i}", title="zephyr") for i in range(8)]
        + [_rec(f"quok{i}", title="quokka") for i in range(8)]
        + [_rec(f"mims{i}", title="mimsy") for i in range(8)]
    )
    records = rich + singles  # N = 29; each facet df = 5 + 8 = 13 -> near-equal IDF

    ranked = _rag_step()._hybrid_rank(question, records, 15)
    pmids = [r["pmid"] for r in ranked]
    assert len(ranked) == 5  # only the multi-coverage records survive
    assert len(ranked) < min(len(records), 15)  # NOT the padded full k the peak gate returns
    assert set(pmids) == {f"rich{i}" for i in range(5)}
    assert not any(p.startswith(("zeph", "quok", "mims")) for p in pmids)  # singles trimmed
    assert pmids[0].startswith("rich")  # multi-coverage records rank at the top


def _coverage_keep_classes(scale: int) -> set[str]:
    """Build a candidate set of FIXED RELATIVE composition at ``scale`` and return the set of
    record class-prefixes the coverage gate keeps.

    Composition (independent of scale): ``rich`` cover all 3 rare facets, ``single`` covers 1
    rare facet, ``common`` covers only the saturated common facet. N = 35 * scale. ``rich`` is
    3*scale so the min-keep floor (3) never pads a trimmed record back in at the small scale.
    """
    question = "zephyr quokka mimsy alpha"
    rich = [_rec(f"rich{i}", title="zephyr quokka mimsy") for i in range(3 * scale)]
    single = [_rec(f"single{i}", title="zephyr") for i in range(2 * scale)]
    common = [_rec(f"common{i}", title="alpha") for i in range(30 * scale)]
    ranked = _rag_step()._hybrid_rank(question, rich + single + common, 15)
    return {r["pmid"].rstrip("0123456789") for r in ranked}


def test_coverage_gate_keep_decision_is_scale_stable():
    """Invariant 4 (SCALE-STABILITY): a record of fixed RELATIVE rarity/coverage gets the SAME
    keep/cut outcome regardless of candidate-set size N. Both the rarity cutoff and the keep
    cutoff are ratios WITHIN the set (never a raw log-N magnitude), so growing N 10x must not
    flip the decision — the 'single' records stay trimmed and the 'common' pile stays dropped
    at both scales.
    """
    small = _coverage_keep_classes(scale=1)  # N = 35
    large = _coverage_keep_classes(scale=10)  # N = 350
    assert small == large == {"rich"}  # single + common trimmed at BOTH scales


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


# --- cycle-3: semantic gate (embedding cosine as a GATE COMPONENT) ---------


def test_semantic_gate_trims_coverage_passing_low_cosine_record():
    """Invariant 5 (SEMANTIC GATE): a coverage-PASSING record that is a cosine outlier is
    trimmed by the semantic bar, so the COMBINED gate returns FEWER records than coverage alone.

    5 records all match the same 3 rare facets -> identical discriminative coverage, so the
    coverage gate alone keeps all 5. Injected cosines make rich4 a semantic outlier (0.1 vs a
    best of 1.0); the within-set bar (_GATE_SEM_FRACTION=0.5 * 1.0 = 0.5) trims ONLY rich4.
    """
    step = _rag_step()
    question = "zephyr quokka mimsy"
    records = [_rec(f"rich{i}", title="zephyr quokka mimsy") for i in range(5)]
    cos = {"rich0": 1.0, "rich1": 0.9, "rich2": 0.8, "rich3": 0.7, "rich4": 0.1}
    step._embed_sims = lambda q, recs: [cos[r["pmid"]] for r in recs]

    ranked = step._hybrid_rank(question, records, 15)
    pmids = {r["pmid"] for r in ranked}
    assert len(ranked) == 4  # coverage alone keeps 5; the semantic bar cuts rich4 -> 4
    assert "rich4" not in pmids  # the semantic outlier is trimmed despite clearing coverage
    assert pmids == {"rich0", "rich1", "rich2", "rich3"}


def test_semantic_gate_skipped_when_embeddings_unavailable():
    """Invariant 6 (SEMANTIC-GATE DEGRADE): when the embedding model cannot load, the semantic
    bar is SKIPPED and the gate == coverage-only (same records coverage keeps), with the existing
    loud warning — never a crash.
    """
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    question = "zephyr quokka mimsy"
    records = [_rec(f"rich{i}", title="zephyr quokka mimsy") for i in range(5)]

    def _raise(q, recs):
        raise RuntimeError("no sentence-transformers")

    step._embed_sims = _raise

    ranked = step._hybrid_rank(question, records, 15)
    pmids = {r["pmid"] for r in ranked}
    assert len(ranked) == 5  # coverage-only: all 5 survive (no semantic trim)
    assert pmids == {f"rich{i}" for i in range(5)}
    assert any("unavailable" in w.lower() for w in step.nb_logger.warnings)  # loud degrade kept


def test_semantic_gate_respects_min_floor():
    """Invariant 7 (SEMANTIC-GATE FLOOR): when the semantic bar alone would cut below the recall
    floor, the floor (min(_GATE_MIN_KEEP=3, n)) still pads back in sort order.

    Only rich0 (cosine 1.0) clears the bar; the other four are outliers (0.1), so the semantic
    gate would yield 1. The floor restores 3 records (top-of-order), rich0 first.
    """
    step = _rag_step()
    question = "zephyr quokka mimsy"
    records = [_rec(f"rich{i}", title="zephyr quokka mimsy") for i in range(5)]
    cos = {"rich0": 1.0, "rich1": 0.1, "rich2": 0.1, "rich3": 0.1, "rich4": 0.1}
    step._embed_sims = lambda q, recs: [cos[r["pmid"]] for r in recs]

    ranked = step._hybrid_rank(question, records, 15)
    assert len(ranked) == 3  # semantic bar alone -> 1; recall floor pads to min(3, n)=3
    assert ranked[0]["pmid"] == "rich0"  # the one bar-clearing record leads the floor-padded set


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
