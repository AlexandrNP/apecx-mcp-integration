"""Unit tests for the opt-in GRADED LLM+CE FUSION stage in LiteratureRagStep.

SYNTHETIC fixtures only — fabricated records, nonsense question tokens, a STUBBED LLM client
(no network, no Ollama, no `rag` extra) and a STUBBED cross-encoder. These exercise the fusion
MECHANISM behind ``APECX_ENABLE_LLM_FUSION``:

  * fusion re-ranks high-LLM+high-CE above low (joint order);
  * a high-CE but near-0-LLM record sinks below the in-pool cut (fixes the blunt hard-override),
    while a high-LLM but mid-CE record is retained (recovers a wrongly-low-CE true positive);
  * the cut is derived from the IN-POOL joint distribution (a saturated pool keeps all — no fixed
    threshold), floor+cap hold, the result is deterministic;
  * the NEW flag default-OFF leaves the pre-fusion output byte-identical (a stub that WOULD change
    fusion has no effect when unset) — and the verify path is untouched by all of this;
  * score parsing is robust (floats, yes/no fallback, garbage -> 0.5);
  * both LLM-failure paths degrade LOUD to the pre-fusion set.

The real-LLM parity partner (CLAUDE.md Mocks Carve-Out) is
tests/integration/test_literature_rag_llm_fusion_against_ollama.py, which drives the SAME
``_hybrid_rank`` fusion path against a real apecx LLM (Ollama) on synthetic toy text.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
    _parse_fusion_score,
)


def _rag_step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    step = LiteratureRagStep.from_config(yml)
    # Neutralize the embedding tiebreak + bi-encoder bar by default; CE-based tests stub
    # _cross_encoder_rerank, the CE-off test overrides _embed_sims with varying cosines.
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    return step


class _CaptureLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):  # pragma: no cover - not asserted
        pass


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """Returns a canned GRADED-score reply per content marker (a record title substring).

    ``replies`` maps a marker -> the raw LLM reply string (e.g. ``"0.95"``); ``invoke`` reads the
    HumanMessage content and returns the first matching reply, exercising the REAL prompt.format +
    _parse_fusion_score path without a network call.
    """

    def __init__(self, replies: dict[str, str], default: str = "0.5"):
        self._replies = replies
        self._default = default

    def invoke(self, messages):
        content = messages[0].content
        for marker, reply in self._replies.items():
            if marker in content:
                return _FakeResponse(reply)
        return _FakeResponse(self._default)


class _RaisingLLM:
    def invoke(self, messages):
        raise RuntimeError("endpoint exploded mid-call")


def _ce_rerank_by_marker(scores: dict[str, float]):
    """Stub for ``_cross_encoder_rerank`` keyed by a record's title marker (signature: q, records)."""

    def _fn(_question, records):
        return [next(v for m, v in scores.items() if m in (r.get("title") or "")) for r in records]

    return _fn


# Common question whose three facets every test record carries, so coverage keeps them all and
# the fusion stage is the only discriminator. Markers (mk_*) are non-facet tokens.
_Q = "zephyr quokka mimsy"


def _rec(pmid: str, title: str = "", abstract: str = "") -> dict:
    return {"pmid": pmid, "title": title, "abstract": abstract}


def _recs(markers: list[str]) -> list[dict]:
    return [_rec(m, title=f"zephyr quokka mimsy {m}") for m in markers]


# --- ranking + the two blunt-override fixes in one realistic CE+LLM scenario ----------------


def test_fusion_orders_by_joint_and_cuts_off_target(monkeypatch):
    """Fusion ranks high-LLM+high-CE above low; a high-CE/near-0-LLM record sinks BELOW the cut
    (the blunt hard-override failure), while a high-LLM/mid-CE record is RETAINED."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_both", "mk_both2", "mk_hllm", "mk_hce"])
    # CE sigmoids all clear the absolute CE gate (>= 0.5). mk_hce has a TOP CE but a near-0 LLM;
    # mk_hllm has a MID CE but a high LLM.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_both": 0.90, "mk_both2": 0.85, "mk_hllm": 0.55, "mk_hce": 0.90}
    )
    step._build_fusion_llm = lambda: _FakeLLM(
        {"mk_both": "0.95", "mk_both2": "0.9", "mk_hllm": "0.95", "mk_hce": "0.05"}
    )

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = [r["pmid"] for r in ranked]
    # joints: both 0.925, both2 0.875, hllm 0.75, hce 0.475 -> dominant gap above 0.475.
    assert pmids == ["mk_both", "mk_both2", "mk_hllm"]  # joint-desc order, hce cut
    assert "mk_hce" not in pmids  # top-CE but off-target sinks below the cut
    assert "mk_hllm" in pmids  # high-LLM/mid-CE true positive retained


def test_ce_off_fuses_with_bi_encoder_cosine(monkeypatch):
    """With CE OFF, the fusion ce_score is the bi-encoder cosine; a low-cosine/low-LLM record
    sinks below the cut. Exercises the ``else [sims[i] ...]`` branch (ce_by_index empty)."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    step = _rag_step()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_lo"])
    # Varying cosines (all clear the relative bi-encoder bar 0.5*best=0.45 so none is pre-trimmed).
    sims_by_marker = {"mk_a": 0.9, "mk_b": 0.8, "mk_c": 0.7, "mk_lo": 0.6}
    step._embed_sims = lambda q, recs: [
        sims_by_marker[next(m for m in sims_by_marker if m in (r.get("title") or ""))] for r in recs
    ]
    step._build_fusion_llm = lambda: _FakeLLM(
        {"mk_a": "0.9", "mk_b": "0.9", "mk_c": "0.9", "mk_lo": "0.1"}
    )

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    # joints: a 0.9, b 0.85, c 0.8, lo 0.35 -> dominant gap above 0.35.
    assert "mk_lo" not in pmids
    assert pmids == {"mk_a", "mk_b", "mk_c"}


# --- in-pool cut: a saturated pool keeps all (no fixed threshold) ---------------------------


def test_saturated_pool_keeps_all_in_pool_cut(monkeypatch):
    """The cut is derived from the in-pool joint distribution: when no gap dominates (all joints
    equal), NOTHING is cut — even though every joint (0.45) is below a naive 0.5 threshold."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)  # ce_score = sims = 0.0
    step = _rag_step()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d"])
    step._build_fusion_llm = lambda: _FakeLLM({}, default="0.9")  # every joint = 0.5*0.9 = 0.45

    ranked = step._hybrid_rank(_Q, records, 15)
    assert {r["pmid"] for r in ranked} == {m["pmid"] for m in records}  # all kept, nothing cut


# --- floor + cap ----------------------------------------------------------------------------


def test_cap_holds_under_fusion(monkeypatch):
    """Even when fusion keeps everything (saturated), the hard cap (k) still holds."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    step = _rag_step()
    records = _recs([f"mk{i}" for i in range(20)])
    step._build_fusion_llm = lambda: _FakeLLM({}, default="0.9")

    ranked = step._hybrid_rank(_Q, records, 15)
    assert len(ranked) == 15  # NOT 20


def test_survivors_below_floor_return_survivors_not_padded(monkeypatch):
    """Precision-first: fusion cuts to 1 survivor < floor(min(3,5)=3) -> return ONLY the survivor;
    the floor must NOT pad back with below-cut records (no-reinject invariant)."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_top", "mk_lo0", "mk_lo1", "mk_lo2", "mk_lo3"])
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_top": 0.90, "mk_lo0": 0.60, "mk_lo1": 0.60, "mk_lo2": 0.60, "mk_lo3": 0.60}
    )
    step._build_fusion_llm = lambda: _FakeLLM({"mk_top": "0.95"}, default="0.05")

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    # joints: top 0.925, lo* 0.325 -> dominant gap, keep only top.
    assert pmids == {"mk_top"}
    assert not (pmids & {"mk_lo0", "mk_lo1", "mk_lo2", "mk_lo3"})  # no-reinject invariant


def test_floor_repopulates_from_survivors_only(monkeypatch):
    """Fusion keeps 4 (>= floor) and drops 2; a below-cut record NEVER re-appears."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_y0", "mk_y1", "mk_y2", "mk_y3", "mk_n0", "mk_n1"])
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_y0": 0.9, "mk_y1": 0.9, "mk_y2": 0.9, "mk_y3": 0.9, "mk_n0": 0.6, "mk_n1": 0.6}
    )
    step._build_fusion_llm = lambda: _FakeLLM(
        {"mk_y0": "0.9", "mk_y1": "0.9", "mk_y2": "0.9", "mk_y3": "0.9"}, default="0.05"
    )

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    assert pmids == {"mk_y0", "mk_y1", "mk_y2", "mk_y3"}  # survivors only
    assert not (pmids & {"mk_n0", "mk_n1"})  # no-reinject invariant


# --- determinism ----------------------------------------------------------------------------


def test_fusion_is_deterministic(monkeypatch):
    """Same inputs -> identical ordered output across repeated runs."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_lo0", "mk_lo1"])
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_a": 0.9, "mk_b": 0.85, "mk_c": 0.8, "mk_lo0": 0.6, "mk_lo1": 0.55}
    )
    step._build_fusion_llm = lambda: _FakeLLM(
        {"mk_a": "0.9", "mk_b": "0.85", "mk_c": "0.8"}, default="0.1"
    )

    first = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    second = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    assert first == second


# --- wiring guard: NEW flag default-OFF is byte-identical to the pre-fusion output ----------


def test_flag_off_is_pre_fusion_and_differs_from_flag_on(monkeypatch):
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_hi", "mk_lo"])

    # Flag OFF: the fusion stage must NOT run at all (scoring would raise if it did).
    monkeypatch.delenv("APECX_ENABLE_LLM_FUSION", raising=False)
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    step_off = _rag_step()

    def _must_not_run(*a, **k):
        raise AssertionError("fusion must NOT run when APECX_ENABLE_LLM_FUSION is unset")

    step_off._llm_relevance_scores = _must_not_run
    pmids_off = {r["pmid"] for r in step_off._hybrid_rank(_Q, records, 15)}
    assert pmids_off == {m["pmid"] for m in records}  # coverage-only pre-fusion keeps all 5

    # Flag ON with a discriminating stub: output DIFFERS (the low-LLM record is cut).
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    step_on = _rag_step()  # ce_score = sims = 0 -> joint = 0.5 * llm
    step_on._build_fusion_llm = lambda: _FakeLLM({"mk_lo": "0.1"}, default="0.9")
    pmids_on = {r["pmid"] for r in step_on._hybrid_rank(_Q, records, 15)}
    assert pmids_on == {"mk_a", "mk_b", "mk_c", "mk_hi"}
    assert pmids_on != pmids_off  # wiring guard: ON changes the result


# --- robust parse of the graded reply -------------------------------------------------------


def test_parse_fusion_score_robust():
    # Leading floats keep their graded value, clamped to [0, 1].
    assert _parse_fusion_score("0.8") == 0.8
    assert _parse_fusion_score("0.05") == 0.05
    assert _parse_fusion_score("1") == 1.0
    assert _parse_fusion_score("0") == 0.0
    assert _parse_fusion_score("  0.73 (relevant)") == 0.73
    assert _parse_fusion_score("1.5") == 1.0  # clamp above
    # A leading "-" is stripped as a markdown bullet (shared verify lstrip), so "-0.3" parses as
    # the magnitude 0.3 — negatives never reach the clamp, but the max(0.0, ...) floor stays as a
    # defensive guard for any value that does arrive negative.
    assert _parse_fusion_score("-0.3") == 0.3
    # yes/no word fallback.
    assert _parse_fusion_score("yes") == 1.0
    assert _parse_fusion_score("relevant") == 1.0
    assert _parse_fusion_score("no") == 0.0
    assert _parse_fusion_score("irrelevant") == 0.0
    # garbage / empty -> 0.5 neutral backstop.
    assert _parse_fusion_score("maybe, hard to say") == 0.5
    assert _parse_fusion_score("") == 0.5
    assert _parse_fusion_score("   ") == 0.5


# --- degrade-loud: LLM unavailable, and every per-call error --------------------------------


def test_fusion_llm_unavailable_degrades_loud_to_pre_fusion(monkeypatch):
    """Client cannot be built -> SKIP fusion, ONE loud warning, return the pre-fusion set."""
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d"])

    def _build_raises():
        raise RuntimeError("ollama unreachable")

    step._build_fusion_llm = _build_raises

    ranked = step._hybrid_rank(_Q, records, 15)
    assert {r["pmid"] for r in ranked} == {m["pmid"] for m in records}  # unchanged pre-fusion set
    assert any(
        "fusion" in w.lower() and "unavailable" in w.lower() for w in step.nb_logger.warnings
    )


def test_every_fusion_call_errors_degrades_loud_to_pre_fusion(monkeypatch):
    """A client that errors on EVERY invoke -> all-None -> SKIP fusion, loud, pre-fusion set.

    Exercises the REAL _llm_relevance_scores per-call except (-> None) plus the all-None branch.
    """
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d"])
    step._build_fusion_llm = lambda: _RaisingLLM()

    ranked = step._hybrid_rank(_Q, records, 15)
    assert {r["pmid"] for r in ranked} == {m["pmid"] for m in records}  # unchanged pre-fusion set
    warns = " ".join(step.nb_logger.warnings).lower()
    assert "every llm fusion score call failed" in warns
