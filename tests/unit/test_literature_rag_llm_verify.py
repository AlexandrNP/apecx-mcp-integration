"""Unit tests for the opt-in LLM relevance-VERIFY filter in LiteratureRagStep.

SYNTHETIC fixtures only — fabricated records, nonsense question tokens, a STUBBED LLM
client (no network, no Ollama, no `rag` extra). These exercise the verify MECHANISM:
a candidate the stub judges "no" is dropped, "yes" is kept, the recall floor is honoured
when the stub rejects most, the flag OFF leaves the pre-verify output untouched, the
response parser is robust, and both LLM-failure paths degrade LOUD to the pre-verify set.

The real-LLM parity partner (CLAUDE.md Mocks Carve-Out) is
tests/integration/test_literature_rag_llm_verify_against_ollama.py, which drives the SAME
``_hybrid_rank`` verify path against a real apecx LLM (Ollama) on synthetic toy text.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
    _parse_verify_verdict,
)


def _ce_model_unavailable():
    raise RuntimeError("cross-encoder model unavailable in unit tests")


def _rag_step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    step = LiteratureRagStep.from_config(yml)
    step._embed_sims = lambda q, recs: [0.0] * len(recs)  # neutralize tiebreak + bi-encoder bar
    step._get_cross_encoder = _ce_model_unavailable  # CE off in units; coverage-only pre-verify
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


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """Stand-in for the apecx ChatOpenAI client: returns a canned reply per content marker.

    ``replies`` maps a marker substring (embedded in a record's title) -> the raw LLM reply
    string. ``invoke`` reads the HumanMessage content and returns the first matching reply,
    exercising the step's REAL prompt.format + parse path without a network call.
    """

    def __init__(self, replies: dict[str, str], default: str = "no"):
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


# Common question whose three facets every test record carries, so coverage keeps them all
# and the verify filter is the ONLY discriminator. Markers (mk_*) are non-facet tokens.
_Q = "zephyr quokka mimsy"


def _recs(markers: list[str]) -> list[dict]:
    return [_rec(m, title=f"zephyr quokka mimsy {m}") for m in markers]


# --- drop "no" / keep "yes" (full format+invoke+parse path through _build_verify_llm) ------


def test_verify_drops_no_keeps_yes(monkeypatch):
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    records = _recs(["mk_y0", "mk_y1", "mk_y2", "mk_n0", "mk_n1"])
    fake = _FakeLLM({"mk_y0": "yes", "mk_y1": "yes", "mk_y2": "yes", "mk_n0": "no", "mk_n1": "no"})
    step._build_verify_llm = lambda: fake

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = [r["pmid"] for r in ranked]
    # 3 "yes" kept == floor(min(3,5)=3), so no padding re-adds a "no"; order preserved.
    assert pmids == ["mk_y0", "mk_y1", "mk_y2"]


def test_unknown_verdict_is_kept(monkeypatch):
    """An unparseable (UNKNOWN) reply keeps the candidate — uncertainty never silently drops."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    records = _recs(["mk_y0", "mk_y1", "mk_q0", "mk_n0"])
    fake = _FakeLLM(
        {
            "mk_y0": "yes",
            "mk_y1": "yes",
            "mk_q0": "I am not sure, it depends",  # no leading verdict -> UNKNOWN -> kept
            "mk_n0": "no",
        }
    )
    step._build_verify_llm = lambda: fake

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = [r["pmid"] for r in ranked]
    assert "mk_n0" not in pmids  # only the confident "no" is dropped
    assert set(pmids) == {"mk_y0", "mk_y1", "mk_q0"}  # UNKNOWN kept alongside the yeses


def test_survivors_below_floor_return_survivors_not_padded(monkeypatch):
    """Fix 1: verify rejects 4 of 5; survivors (1) < floor (min(3,5)=3) -> return ONLY the
    survivor(s). The floor must NOT pad back up with LLM-rejected records (precision-first:
    a smaller, clean set is correct). The no-reinject invariant: no rejected record appears."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    records = _recs(["mk_y0", "mk_n0", "mk_n1", "mk_n2", "mk_n3"])
    fake = _FakeLLM({"mk_y0": "yes"}, default="no")
    step._build_verify_llm = lambda: fake

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    # Exactly the lone survivor — NOT padded to the floor, NO rejected record re-injected.
    assert pmids == {"mk_y0"}
    assert not (pmids & {"mk_n0", "mk_n1", "mk_n2", "mk_n3"})  # no-reinject invariant


def test_floor_repopulates_from_survivors_only(monkeypatch):
    """Fix 1: verify rejects most but survivors (4) >= floor -> keep exactly the survivors; a
    stub-REJECTED record NEVER appears in the output (the no-reinject invariant)."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    records = _recs(["mk_y0", "mk_y1", "mk_y2", "mk_y3", "mk_n0", "mk_n1"])
    fake = _FakeLLM({"mk_y0": "yes", "mk_y1": "yes", "mk_y2": "yes", "mk_y3": "yes"}, default="no")
    step._build_verify_llm = lambda: fake

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    assert pmids == {"mk_y0", "mk_y1", "mk_y2", "mk_y3"}  # survivors only
    assert not (pmids & {"mk_n0", "mk_n1"})  # no-reinject invariant: rejected never return


def test_cap_holds_under_verify(monkeypatch):
    """Even when verify keeps everything, the hard cap (k) still holds."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    records = _recs([f"mk_y{i}" for i in range(20)])
    step._build_verify_llm = lambda: _FakeLLM({}, default="yes")  # all relevant

    ranked = step._hybrid_rank(_Q, records, 15)
    assert len(ranked) == 15  # NOT 20


# --- Fix 2: protect top retrieval-confidence tier from weak-verifier over-rejection ---------


def _ce_rerank_by_marker(scores: dict[str, float]):
    """Stub for ``_cross_encoder_rerank``: score each record by the CE value for its title marker.

    Assigned as an instance attribute, so the signature is ``(question, records)`` (no ``self``).
    Every value is >= 0.5 so all survive the absolute CE gate and reach the verify stage, where the
    IN-POOL quantile decides which are protected.
    """

    def _fn(_question, records):
        return [next(v for m, v in scores.items() if m in (r.get("title") or "")) for r in records]

    return _fn


def test_top_ce_survives_no_low_ce_dropped(monkeypatch):
    """Fix 2: a top retrieval-confidence (CE) record SURVIVES a confident LLM "no"; a low-CE
    record with the SAME "no" is DROPPED. The tier is the top quartile of the in-pool CE scores."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_hi", "mk_lo0", "mk_lo1", "mk_lo2"])
    # CE sigmoids: one clear top (0.95) + a low cluster. 75th pctile of [.51,.52,.55,.95] = 0.65,
    # so ONLY mk_hi (0.95) is in the top tier; the rest are the uncertain tail.
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi": 0.95, "mk_lo0": 0.55, "mk_lo1": 0.52, "mk_lo2": 0.51}
    )
    step._build_verify_llm = lambda: _FakeLLM({}, default="no")  # LLM says "no" to EVERY candidate

    ranked = step._hybrid_rank(_Q, records, 15)
    pmids = {r["pmid"] for r in ranked}
    assert "mk_hi" in pmids  # top-tier retrieval hit survives the LLM "no"
    assert not (pmids & {"mk_lo0", "mk_lo1", "mk_lo2"})  # uncertain tail dropped on "no"


def test_verify_is_deterministic(monkeypatch):
    """Determinism: same inputs -> identical ordered output across repeated runs (both fixes on)."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    monkeypatch.setenv("APECX_ENABLE_CROSS_ENCODER", "1")
    step = _rag_step()
    records = _recs(["mk_hi", "mk_lo0", "mk_lo1", "mk_n0", "mk_n1"])
    step._cross_encoder_rerank = _ce_rerank_by_marker(
        {"mk_hi": 0.95, "mk_lo0": 0.6, "mk_lo1": 0.58, "mk_n0": 0.55, "mk_n1": 0.52}
    )
    step._build_verify_llm = lambda: _FakeLLM(
        {"mk_hi": "no", "mk_n0": "no", "mk_n1": "no"}, default="yes"
    )

    first = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    second = [r["pmid"] for r in step._hybrid_rank(_Q, records, 15)]
    assert first == second  # byte-identical order on repeat


# --- wiring guard: flag OFF is identical to the pre-verify output --------------------------


def test_flag_off_is_pre_verify_and_differs_from_flag_on(monkeypatch):
    records = _recs(["mk_y0", "mk_y1", "mk_y2", "mk_y3", "mk_n0", "mk_n1"])

    # Flag OFF: the verify stage must NOT run at all (building the client would raise).
    monkeypatch.delenv("APECX_ENABLE_LLM_VERIFY", raising=False)
    step_off = _rag_step()

    def _must_not_run(*a, **k):
        raise AssertionError("verify must NOT run when APECX_ENABLE_LLM_VERIFY is unset")

    step_off._llm_relevance_verdicts = _must_not_run
    pmids_off = [r["pmid"] for r in step_off._hybrid_rank(_Q, records, 15)]
    assert set(pmids_off) == {m["pmid"] for m in records}  # coverage-only pre-verify keeps all 6

    # Flag ON with a discriminating stub: output DIFFERS (the two "no" records are trimmed).
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step_on = _rag_step()
    step_on._build_verify_llm = lambda: _FakeLLM({"mk_n0": "no", "mk_n1": "no"}, default="yes")
    pmids_on = [r["pmid"] for r in step_on._hybrid_rank(_Q, records, 15)]
    assert set(pmids_on) == {"mk_y0", "mk_y1", "mk_y2", "mk_y3"}
    assert set(pmids_on) != set(pmids_off)  # wiring guard: ON changes the result


# --- prompt-contract guard: generic template, placeholders, yes/no output ------------------


def test_verify_prompt_is_generic_and_asks_yes_no():
    """The verify prompt stays parameterized by {question}/{title}/{abstract}, asks for a
    leading yes/no (the contract `_parse_verify_verdict` reads), and carries NO domain terms.
    Loosened-rubric wording ("relevant", "off-topic") replaces the old strict "satisfies"."""
    import apecx_integration.composition.workflows.literature_rag.steps as pkg

    text = (Path(pkg.__file__).resolve().parent / "verify_prompt.md").read_text(encoding="utf-8")
    low = text.lower()

    # Generic placeholders are present (the step's prompt.format fills these).
    for ph in ("{question}", "{title}", "{abstract}"):
        assert ph in text, ph
    # Asks for a yes/no verdict (the leading-word contract the parser consumes).
    assert "yes" in low and "no" in low
    # Loosened rubric: relevance-based, not the old strict "satisfies the question".
    assert "relevant" in low
    assert "off-topic" in low
    assert "satisfies the question" not in low
    # Stays generic: no domain/organism/PMID specifics leaked into the template.
    for domain_term in ("virus", "epitope", "pmid", "antigen", "protein"):
        assert domain_term not in low, domain_term


# --- robust parse of the LLM reply --------------------------------------------------------


def test_parse_verify_verdict_robust():
    for yes in [
        "yes",
        "Yes",
        "YES.",
        "y",
        "true",
        "True",
        "relevant",
        "Relevant - ...",
        "1",
        "1.0",
    ]:
        assert _parse_verify_verdict(yes) is True, yes
    for no in ["no", "No", "NO.", "n", "false", "False", "irrelevant", "unrelated", "0", "0.0"]:
        assert _parse_verify_verdict(no) is False, no
    assert _parse_verify_verdict("0.8") is True  # leading score >= 0.5
    assert _parse_verify_verdict("0.2") is False  # leading score < 0.5
    assert _parse_verify_verdict("") is None  # empty -> UNKNOWN
    assert _parse_verify_verdict("   ") is None
    assert _parse_verify_verdict("maybe, it is hard to say") is None  # no leading verdict


# --- degrade-loud: LLM unavailable, and every per-call error ------------------------------


def test_llm_unavailable_degrades_loud_to_pre_verify(monkeypatch):
    """Client cannot be built -> SKIP the filter, ONE loud warning, return the pre-verify set."""
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d"])

    def _build_raises():
        raise RuntimeError("ollama unreachable")

    step._build_verify_llm = _build_raises

    ranked = step._hybrid_rank(_Q, records, 15)
    assert {r["pmid"] for r in ranked} == {m["pmid"] for m in records}  # unchanged pre-verify set
    assert any(
        "verify" in w.lower() and "unavailable" in w.lower() for w in step.nb_logger.warnings
    )


def test_every_call_errors_degrades_loud_to_pre_verify(monkeypatch):
    """A client that errors on EVERY invoke -> all-UNKNOWN -> SKIP filter, loud, pre-verify set.

    Exercises the REAL _llm_relevance_verdicts per-call except (-> None) plus the all-None branch.
    """
    monkeypatch.setenv("APECX_ENABLE_LLM_VERIFY", "1")
    step = _rag_step()
    step.nb_logger = _CaptureLogger()
    records = _recs(["mk_a", "mk_b", "mk_c", "mk_d"])
    step._build_verify_llm = lambda: _RaisingLLM()

    ranked = step._hybrid_rank(_Q, records, 15)
    assert {r["pmid"] for r in ranked} == {m["pmid"] for m in records}  # unchanged pre-verify set
    warns = " ".join(step.nb_logger.warnings).lower()
    assert "every llm verify call failed" in warns  # the all-errored degrade branch warned loud
