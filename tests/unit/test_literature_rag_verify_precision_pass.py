"""Unit tests for the opt-in VERIFY-PRECISION PASS in LiteratureRagStep.

SYNTHETIC fixtures only — fabricated records, nonsense question tokens, a STUBBED cross-encoder
and a STUBBED verify LLM (no network, no Ollama, no ``rag`` extra). These exercise the MECHANISM
behind ``APECX_ENABLE_VERIFY_PRECISION_PASS``: on the verify-SURVIVORS it reuses
``_fusion_cut`` (dominant-gap) to shed a thin low-confidence tail while leaving a tight cluster
untouched, honours the recall floor + the hard cap, never re-injects a verify-dropped record, is
deterministic, is byte-identical to the plain verify path when the flag is OFF, and never runs when
verify itself is OFF.

The pass is deterministic and LLM-free (it reuses the CE/bi-encoder confidence already computed in
the rank), so there is no real-LLM parity partner to add — the mocked surface here (CE scores, verify
verdicts) is identical to the surface the existing verify integration test
(tests/integration/test_literature_rag_llm_verify_against_ollama.py) drives against a real apecx LLM.
"""

from __future__ import annotations

from pathlib import Path


def _rag_step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    step = LiteratureRagStep.from_config(yml)
    step._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize the bi-encoder tiebreak/bar
    return step


def _rec(pmid: str, title: str = "", abstract: str = "") -> dict:
    return {"pmid": pmid, "title": title, "abstract": abstract}


# Common question whose three facets every record carries, so coverage keeps them all and the
# verify + precision pass are the only discriminators. Markers (mk_*) are non-facet title tokens.
_Q = "zephyr quokka mimsy"


def _recs(markers: list[str]) -> list[dict]:
    return [_rec(m, title=f"zephyr quokka mimsy {m}") for m in markers]


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, replies: dict[str, str], default: str = "yes"):
        self._replies = replies
        self._default = default

    def invoke(self, messages):
        content = messages[0].content
        for marker, reply in self._replies.items():
            if marker in content:
                return _FakeResponse(reply)
        return _FakeResponse(self._default)


def _ce_rerank_by_marker(scores: dict[str, float]):
    """Stub for ``_cross_encoder_rerank`` keyed by a record's title marker (signature: q, records)."""

    def _fn(_question, records):
        return [next(v for m, v in scores.items() if m in (r.get("title") or "")) for r in records]

    return _fn


def _enable_verify_and_ce(monkeypatch):
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")


# --- core: well-separated low tail dropped; tight cluster kept -------------------------------


def test_well_separated_low_tail_is_dropped(monkeypatch):
    """A verify-survivor in a thin low-confidence tail separated by a dominant gap is dropped."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs(["mk_hi0", "mk_hi1", "mk_hi2", "mk_lo"])
    # CE sigmoids all >= 0.5 (pass the CE gate) and verify keeps all (default "yes"). Sorted desc
    # [0.95, 0.93, 0.90, 0.55]: the 0.35 gap below 0.90 dominates (> half the 0.40 descent) -> cut at
    # 0.90 -> the 0.55 tail is shed. 3 survivors == floor(min(3,4)=3), so no floor re-add.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi0": 0.95, "mk_hi1": 0.93, "mk_hi2": 0.90, "mk_lo": 0.55}
    )
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    pmids = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    assert pmids == ["mk_hi0", "mk_hi1", "mk_hi2"]  # order preserved, low tail shed


def test_tight_cluster_is_fully_kept(monkeypatch):
    """A tight (no-dominant-gap) cluster of verify-survivors is kept untouched — the recall win
    is preserved: no dominant gap -> no trim (conservative by construction)."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d", "mk_e"])
    # Sorted desc [0.90, 0.88, 0.86, 0.84, 0.82]: every gap 0.02, none exceeds the 0.06 rest -> keep
    # all five.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_a": 0.90, "mk_b": 0.88, "mk_c": 0.86, "mk_d": 0.84, "mk_e": 0.82}
    )
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    pmids = {r["pmid"] for r in step._hybrid_rank(_Q, records, 15)}
    assert pmids == {"mk_a", "mk_b", "mk_c", "mk_d", "mk_e"}  # tight cluster -> no trim


# --- floor-from-survivors + no-reinject -----------------------------------------------------


def test_floor_repopulates_from_survivors(monkeypatch):
    """When the dominant-gap cut would drop below the recall floor, pad back the highest-confidence
    verify-survivors up to the floor (never below it)."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs(["mk_hi", "mk_m0", "mk_m1", "mk_lo"])
    # Sorted desc [0.95, 0.55, 0.52, 0.51]: the 0.40 top gap dominates -> cut keeps only mk_hi (1).
    # floor = min(3, 4) = 3 -> pad back the two highest-confidence trimmed survivors (0.55, 0.52);
    # the lowest (0.51) stays shed.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi": 0.95, "mk_m0": 0.55, "mk_m1": 0.52, "mk_lo": 0.51}
    )
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    pmids = {r["pmid"] for r in step._hybrid_rank(_Q, records, 15)}
    assert pmids == {"mk_hi", "mk_m0", "mk_m1"}  # floor honoured; the lowest tail record shed
    assert "mk_lo" not in pmids


def test_no_reinject_of_verify_dropped_record(monkeypatch):
    """The precision floor pads back ONLY from verify-SURVIVORS — a verify-dropped record never
    returns, even when the pass must repopulate to meet the floor."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d", "mk_n"])
    # CE sorted [0.95, 0.90, 0.60, 0.58, 0.56]. 75th pctile = 0.90 -> top tier {a, b}. Verify says
    # "no" to mk_n (CE 0.56, NOT top tier) -> dropped; survivors {a, b, c, d}. Precision on
    # [0.95, 0.90, 0.60, 0.58]: 0.30 gap below 0.90 dominates -> keep {a, b} (2); floor 3 -> pad back
    # c (0.60). mk_n (verify-dropped) must NOT be re-injected; d stays shed by the precision cut.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_a": 0.95, "mk_b": 0.90, "mk_c": 0.60, "mk_d": 0.58, "mk_n": 0.56}
    )
    step._build_verify_llm = lambda: _FakeLLM({"mk_n": "no"}, default="yes")

    pmids = {r["pmid"] for r in step._hybrid_rank(_Q, records, 15)}
    assert pmids == {"mk_a", "mk_b", "mk_c"}
    assert "mk_n" not in pmids  # verify-dropped record never re-injected by the precision floor


# --- cap + determinism ----------------------------------------------------------------------


def test_cap_holds_under_precision_pass(monkeypatch):
    """The hard cap (k) still holds when the tight-cluster pass keeps everything."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs([f"mk_{i}" for i in range(20)])
    # All equal CE -> no gap -> keep all -> cap trims to k.
    step._cross_encoder_rerank = _ce_rerank_by_marker({f"mk_{i}": 0.9 for i in range(20)})
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    assert len(step._hybrid_rank(_Q, records, 15)) == 15  # NOT 20


def test_precision_pass_is_deterministic(monkeypatch):
    """Same inputs -> identical ordered output across repeated runs."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")
    step = _rag_step()
    records = _recs(["mk_hi0", "mk_hi1", "mk_hi2", "mk_lo0", "mk_lo1"])
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi0": 0.95, "mk_hi1": 0.93, "mk_hi2": 0.91, "mk_lo0": 0.55, "mk_lo1": 0.52}
    )
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    first = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    second = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    assert first == second


# --- flag OFF is byte-identical to the plain verify path ------------------------------------


def test_flag_off_is_byte_identical_to_verify(monkeypatch):
    """With the pass flag UNSET the verify output is byte-identical: a stub that WOULD change the
    pass (``_fusion_cut`` raising) is never reached, because the pass block never runs."""
    _enable_verify_and_ce(monkeypatch)
    monkeypatch.delenv("APECX_ENABLE_VERIFY_PRECISION_PASS", raising=False)
    records = _recs(["mk_hi0", "mk_hi1", "mk_hi2", "mk_lo"])
    ce = {"mk_hi0": 0.95, "mk_hi1": 0.93, "mk_hi2": 0.90, "mk_lo": 0.55}

    step = _rag_step()
    step._cross_encoder_rerank = _ce_rerank_by_marker(ce)
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")

    def _must_not_run(*a, **k):
        raise AssertionError("precision pass must NOT run when the flag is unset")

    step._fusion_cut = _must_not_run  # would blow up IF the pass ran
    pmids = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    # Verify kept all four (default "yes"); the low tail is NOT shed because the pass is OFF.
    assert pmids == ["mk_hi0", "mk_hi1", "mk_hi2", "mk_lo"]


# --- pass never runs when verify is OFF -----------------------------------------------------


def test_pass_never_runs_when_verify_off(monkeypatch):
    """Verify OFF (-> verify_applied False) -> the precision pass never runs, even with its own
    flag ON: a ``_fusion_cut`` stub that would trim is never reached, and the pre-verify set stands."""
    monkeypatch.delenv("APECX_ENABLE_LLM_VERIFY", raising=False)  # verify OFF
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    monkeypatch.setenv("APECX_ENABLE_VERIFY_PRECISION_PASS", "1")  # pass ON, but verify gates it
    step = _rag_step()
    records = _recs(["mk_hi0", "mk_hi1", "mk_hi2", "mk_lo"])
    # CE values all clear the absolute bar so coverage+CE keep all four pre-verify.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi0": 0.95, "mk_hi1": 0.93, "mk_hi2": 0.90, "mk_lo": 0.55}
    )

    def _must_not_run(*a, **k):
        raise AssertionError("precision pass must NOT run when verify is OFF")

    step._fusion_cut = _must_not_run
    pmids = {r["pmid"] for r in step._hybrid_rank(_Q, records, 15)}
    assert pmids == {"mk_hi0", "mk_hi1", "mk_hi2", "mk_lo"}  # pre-verify set, pass never ran
