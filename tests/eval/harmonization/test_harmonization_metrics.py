"""Pure-logic unit tests for the harmonization precision/recall metrics + non-circular judges.

No network, no dictionary — every test runs offline against hand-built DataCite records and id sets.
The load-bearing test is ``test_judge_a_does_not_read_valueuri``: it proves the judge decides from the
SOURCE taxon id, not the filtered ``subjects.valueUri`` field — the non-circularity the whole eval rests
on. (This IS allowed to be a ``test_*.py`` — it is pure logic, per the eval-scaffolding carve-out.)
"""

from __future__ import annotations

from tests.eval.harmonization import judges, metrics

# ---- DataCite record fixtures (minimal shapes the real _datacite readers parse) -------------------


def _rec(*, title=None, desc=None, subjects=None, ncbi=None, organisms=None):
    """subjects = list of (subject_text, valueUri); ncbi = list of NCBI-Taxonomy id strings."""
    c: dict = {}
    if title is not None:
        c["titles"] = [{"title": title}]
    if desc is not None:
        c["descriptions"] = [{"description": desc}]
    if subjects is not None:
        c["subjects"] = [{"subject": s, "valueUri": u} for s, u in subjects]
    if ncbi is not None:
        c["alternateIdentifiers"] = [
            {"alternateIdentifier": v, "alternateIdentifierType": "NCBI-Taxonomy"} for v in ncbi
        ]
    if organisms is not None:
        c["pdb"] = {"polymer_entities": [{"scientific_name": o} for o in organisms]}
    return c


_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"  # chikungunya species


# ---- metrics.recall_fractions --------------------------------------------------------------------


def test_recall_fractions_matches_hand_computed():
    raw = {"a", "b"}
    harm = {"a", "b", "c", "d"}
    served = {"a", "b", "c", "d"}
    gold = {"a", "b", "c", "d", "e"}  # |gold| = 5
    r = metrics.recall_fractions(raw, harm, served, gold)
    assert r["before"] == 0.4 and r["after"] == 0.8 and r["served"] == 0.8
    assert r["lift"] == 0.4


def test_recall_fractions_empty_gold_returns_none():
    r = metrics.recall_fractions({"a"}, {"a"}, {"a"}, set())
    assert r == {"before": None, "after": None, "served": None, "lift": None}


# ---- metrics.precision (unjudgeable excluded) ----------------------------------------------------


def test_precision_excludes_unjudgeable():
    v = ["relevant", "relevant", "relevant", "false_positive", "unjudgeable", "unjudgeable"]
    p = metrics.precision(v)
    assert p["judged"] == 4 and p["precision"] == 0.75  # 3/4, the 2 unjudgeable not in denominator
    assert p["unjudgeable"] == 2 and p["unjudgeable_rate"] == round(2 / 6, 4)


def test_precision_all_unjudgeable_is_none():
    p = metrics.precision(["unjudgeable", "unjudgeable"])
    assert p["precision"] is None and p["judged"] == 0


def test_f1():
    assert metrics.f1(0.8, 0.5) == round(2 * 0.8 * 0.5 / 1.3, 4)
    assert metrics.f1(None, 0.5) is None
    assert metrics.f1(0.0, 0.0) is None


# ---- judges.judge_a (source taxonomy, independent of valueUri) -----------------------------------


def test_judge_a_in_subtree_true():
    rec = _rec(ncbi=["37124"])
    assert judges.judge_a(rec, {37124, 999001}) is True


def test_judge_a_off_target_false():
    rec = _rec(ncbi=["11021"])  # EEEV — not in the CHIKV subtree
    assert judges.judge_a(rec, {37124}) is False


def test_judge_a_no_source_id_none():
    rec = _rec(organisms=["Chikungunya virus"])  # structural: no NCBI-Taxonomy id
    assert judges.judge_a(rec, {37124}) is None


def test_judge_a_does_not_read_valueuri():
    """NON-CIRCULARITY: a record whose subjects.valueUri IS the queried species (37124) but whose
    SOURCE NCBI-Taxonomy id is a DIFFERENT organism (99999, off-target) must be judged False — proving
    the judge reads the source id, never the filtered valueUri field."""
    rec = _rec(subjects=[("chikungunya", _IRI)], ncbi=["99999"])
    assert judges.judge_a(rec, {37124}) is False  # 99999 ∉ subtree, despite valueUri==37124


# ---- judges.judge_b (descriptive text, independent of taxonomy integers) -------------------------


def test_judge_b_synonym_hit_in_title():
    rec = _rec(title="Chikungunya virus strain X genome")
    assert judges.judge_b(rec, ("chikungunya virus", "chikungunya", "CHIKV")) is True


def test_judge_b_word_bounded():
    rec = _rec(title="Behavioral study of hepatitis")  # 'HAV' must NOT match 'Behavioral'
    assert judges.judge_b(rec, ("HAV",)) is None


def test_judge_b_no_text_none():
    assert judges.judge_b(_rec(), ("chikungunya",)) is None


# ---- judges.combined_verdict (the (a,b) matrix) --------------------------------------------------


def test_combined_verdict_matrix():
    T, F, N = True, False, None
    expect = {
        (T, T): "relevant",
        (T, N): "relevant",
        (N, T): "relevant",
        (F, F): "false_positive",
        (F, N): "false_positive",
        (N, F): "false_positive",
        (T, F): "disagree",
        (F, T): "disagree",
        (N, N): "unjudgeable",
    }
    for (a, b), want in expect.items():
        assert judges.combined_verdict(a, b) == want, f"({a},{b})"


# ---- judges.classify_fp --------------------------------------------------------------------------


def test_classify_fp_raw_substitution():
    rec = _rec(ncbi=["99999"], subjects=[("x", _IRI)])
    assert judges.classify_fp(rec, served_from_raw=True, valueuri_count=1) == "raw_substitution"


def test_classify_fp_multi_subject():
    rec = _rec(ncbi=["99999"])
    assert (
        judges.classify_fp(rec, served_from_raw=False, valueuri_count=3)
        == "multi_subject_incidental"
    )


def test_classify_fp_structural():
    rec = _rec(organisms=["West Nile virus"])  # no source id, organism text present
    assert (
        judges.classify_fp(rec, served_from_raw=False, valueuri_count=1) == "structural_text_parse"
    )


def test_classify_fp_mis_resolution():
    rec = _rec(ncbi=["99999"])  # single-subject, has source id, off-target stamp
    assert judges.classify_fp(rec, served_from_raw=False, valueuri_count=1) == "mis_resolution"


# ---- metrics.judge_agreement (accuracy + Cohen kappa) --------------------------------------------


def test_judge_agreement_perfect():
    pairs = [
        ("relevant", True),
        ("false_positive", False),
        ("relevant", True),
        ("false_positive", False),
    ]
    a = metrics.judge_agreement(pairs)
    assert a["accuracy"] == 1.0 and a["kappa"] == 1.0 and a["n"] == 4


def test_judge_agreement_drops_llm_abstain():
    pairs = [("relevant", True), ("relevant", None), ("false_positive", False)]
    a = metrics.judge_agreement(pairs)
    assert a["n"] == 2 and a["llm_abstained"] == 1


def test_judge_agreement_kappa_below_one():
    # 3 agree, 1 disagree → accuracy 0.75; kappa < 1.
    pairs = [("relevant", True), ("relevant", True), ("false_positive", False), ("relevant", False)]
    a = metrics.judge_agreement(pairs)
    assert a["accuracy"] == 0.75 and a["kappa"] is not None and a["kappa"] < 1.0


# ---- metrics.aggregate (micro-mean) --------------------------------------------------------------


def test_coverage_by_index():
    cells = [
        {
            "index": "bvbrc_genome",
            "term": "CHIKV",
            "harm_total": 6684,
            "served_verdict": "harmonization_helped",
        },
        {
            "index": "bvbrc_genome",
            "term": "EEEV",
            "harm_total": 895,
            "served_verdict": "harmonization_helped",
        },
        {"index": "bvbrc_genome", "term": "Zika", "harm_total": 0, "served_verdict": "broken"},
        {
            "index": "antiviraldb",
            "term": "CHIKV",
            "harm_total": 0,
            "served_verdict": "zero_floor_unclear",
        },
        {
            "index": "antiviraldb",
            "term": "EEEV",
            "harm_total": 0,
            "served_verdict": "zero_floor_unclear",
        },
        {
            "index": "antiviraldb",
            "term": "Zika",
            "harm_total": 0,
            "served_verdict": "zero_floor_unclear",
        },
    ]
    cov = metrics.coverage_by_index(cells, n_resolved=3)
    assert cov["bvbrc_genome"]["pathogens_covered"] == 2  # CHIKV + EEEV (Zika harm=0)
    assert cov["bvbrc_genome"]["coverage_rate"] == round(2 / 3, 4)
    assert cov["bvbrc_genome"]["verdicts"]["harmonization_helped"] == 2
    assert (
        cov["antiviraldb"]["pathogens_covered"] == 0 and cov["antiviraldb"]["coverage_rate"] == 0.0
    )


def test_aggregate_reports_unjudgeable_rate():
    cells = [
        {
            "category": "abbr",
            "precision": {"relevant": 8, "judged": 10, "unjudgeable": 5},
            "recall": {"lift": 0.4},
        }
    ]
    agg = metrics.aggregate(cells, "category")
    assert agg["abbr"]["unjudgeable_rate"] == round(5 / 15, 4)  # 5 unjudgeable of 15 seen


def test_aggregate_micro_mean_by_category():
    cells = [
        {"category": "abbr", "precision": {"relevant": 8, "judged": 10}, "recall": {"lift": 0.4}},
        {"category": "abbr", "precision": {"relevant": 4, "judged": 10}, "recall": {"lift": 0.2}},
        {"category": "real", "precision": {"relevant": 9, "judged": 10}, "recall": {"lift": None}},
    ]
    agg = metrics.aggregate(cells, "category")
    assert agg["abbr"]["precision"] == 0.6 and agg["abbr"]["judged"] == 20  # (8+4)/(10+10)
    assert agg["abbr"]["mean_recall_lift"] == round(0.3, 4)
    assert agg["real"]["precision"] == 0.9 and agg["real"]["mean_recall_lift"] is None
