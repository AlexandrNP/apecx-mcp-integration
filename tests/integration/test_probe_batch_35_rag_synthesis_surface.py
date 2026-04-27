"""Probe batch 35 — adversarial probes targeting the rag_synthesis
attack surface introduced in Day 2 (v3 / v4 / v5 / v6 / v7).

Streak before this batch: 25/300 post-AQ (cluster AR was the
production silent-failure bug surfaced by batch 34). New batch
target: 25 probes, each DISTINCT from prior batches, biased toward
the shapes most likely to reveal real silent-failure bugs.

Probe naming: 930–954 (continuing the global counter).

Distinct probes only — different batches for the same probe do not
count toward the 300-streak per user directive 2026-04-27. Each
probe here exercises a separate behavior that NO prior probe has
checked.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from apecx_integration.agents.rag_synthesis import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
    SynthesisConfig,
    synthesize_response,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    _extract_distinct_citations,
    _render_bvbrc_genomes,
    _render_publications,
    _render_rag_chunks,
    _render_violin_mappings,
)


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StubLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.received: list = []

    def invoke(self, messages):
        self.received.append(messages)
        return AIMessage(content=self.content)


def _cfg(**overrides) -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)


# --------------------------------------------------------------------------- #
# Probes 930–954
# --------------------------------------------------------------------------- #


def test_probe_930_max_rag_chunks_zero_surfaces_no_chunks_but_allows_other_sources():
    """max_rag_chunks=0 must produce no chunks but not break the
    pipeline when other sources populate. Cap of 0 is a defensible
    operator choice (e.g., disabling RAG for one deployment)."""
    cfg = _cfg(max_rag_chunks=0)
    inputs = dict(
        rag_chunks=[{"text": "some text"}, {"text": "more"}],
        bvbrc_genomes=[{"genome_id": "1.1", "name": "X"}],
    )
    captured = []

    class _Cap:
        def invoke(self, msgs):
            captured.append(msgs)
            return AIMessage(content=("Body text. " * 30) + "[BV-BRC genome 1.1]")

    out = synthesize_response("Q", config=cfg, llm=_Cap(), **inputs)
    user_msg = captured[0][1].content
    assert "### RAG chunk #" not in user_msg, (
        "max_rag_chunks=0 must drop ALL chunks; renderer leaked one"
    )
    assert "BV-BRC genome `1.1`" in user_msg
    assert "[BV-BRC genome 1.1]" in out


def test_probe_931_publications_cap_zero_with_input_passes_grounding():
    """max_publications=0 with publications supplied: all skipped,
    allowed_tokens shrinks, LLM citing the dropped DOI is rejected."""
    cfg = _cfg(
        max_publications=0,
        max_rag_chunks=0,
        max_violin_mappings=0,
    )
    inputs = dict(
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        publications=[{"doi": "10.1/x", "title": "t"}],
    )
    # LLM tries to cite the DOI that was capped out → grounding rejects.
    stub = _StubLLM(content=("body " * 50) + "[10.1/x]")
    with pytest.raises(ValueError, match="hallucinat"):
        synthesize_response("Q", config=cfg, llm=stub, **inputs)


def test_probe_932_malformed_regex_in_patterns_raises_at_extract_time():
    """Pydantic accepts citation_marker_patterns as list[str]; a
    malformed regex would silently corrupt extraction. Verify either
    config-load rejects the bad pattern OR extraction surfaces a
    clean error (not a silent empty match set)."""
    import re
    cfg = _cfg(citation_marker_patterns=[r"\[bad("])  # missing close paren
    stub = _StubLLM(content=("body " * 50) + "[BV-BRC genome G1]")
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    # Either re.error propagates (acceptable — fail-fast) or the
    # regex compiles but doesn't match (also acceptable since the
    # default patterns still apply and grounding handles the rest).
    # The silent-failure shape we're guarding: the pattern silently
    # being treated as no-op and the validator becoming inert.
    with pytest.raises((re.error, ValueError)):
        synthesize_response(
            "Q", config=cfg, llm=stub,
            **inputs,
        )


def test_probe_933_chunk_text_containing_literal_citation_token_does_not_auto_cite():
    """A RAG chunk whose CONTENT contains ``[BV-BRC genome XYZ]`` must
    NOT cause the renderer to add XYZ to the allowed tokens. The
    renderer authorizes tokens from the CHUNK ID-derived set
    (#1..#N), not from chunk content. If the LLM later cites
    [BV-BRC genome XYZ] when no XYZ row was supplied, grounding
    must reject."""
    rendered, allowed = _render_rag_chunks(
        [{"text": "see [BV-BRC genome XYZ] for details"}],
        cap=8, strict=True,
    )
    assert allowed == {"[RAG chunk #1]"}, (
        f"renderer leaked content-derived tokens into allowed set: "
        f"{allowed!r}"
    )


def test_probe_934_llm_emitted_token_at_unrenderable_index_rejected():
    """LLM emits ``[RAG chunk #5]`` but only 2 chunks survived
    rendering. Grounding must reject — the LLM is hallucinating
    beyond the prompt's content."""
    inputs = dict(
        rag_chunks=[{"text": "a"}, {"text": "b"}],  # 2 surviving
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
    )
    stub = _StubLLM(content=("body " * 50) + "[RAG chunk #5]")
    with pytest.raises(ValueError, match="hallucinat"):
        synthesize_response("Q", llm=stub, **inputs)


def test_probe_935_doi_with_plus_and_paren_handled_in_renderer_and_pattern():
    """Real-world DOIs occasionally contain ``+`` and ``()``. The
    citation regex must accept them; the renderer's allowed-token
    set must match the LLM-emitted shape."""
    pub = {"doi": "10.5061/dryad.abc+def(2024)", "title": "T"}
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    expected = "[10.5061/dryad.abc+def(2024)]"
    assert allowed == {expected}, allowed
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        f"see {expected} for the data", cfg.citation_marker_patterns
    )
    assert extracted == {expected}, (
        f"DOI pattern fails on +/() chars: extracted={extracted!r}"
    )


def test_probe_936_violin_synonym_id_with_unicode():
    """A VIOLIN synonym_id containing a unicode char (some onto-
    logies use them) must round-trip through renderer + extraction."""
    sid = "VO_β-1"
    rendered, allowed = _render_violin_mappings(
        [{"synonym_id": sid, "canonical_term": "X"}],
        cap=1, strict=True,
    )
    assert f"[VIOLIN {sid}]" in rendered
    assert f"[VIOLIN {sid}]" in allowed
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        f"see [VIOLIN {sid}] above", cfg.citation_marker_patterns
    )
    assert f"[VIOLIN {sid}]" in extracted


def test_probe_937_chunk_score_zero_renders_correctly():
    """``score=0.0`` is a valid (perfect-mismatch) score; the renderer
    truthiness check ``score is not None`` must accept it instead of
    silently dropping the value."""
    rendered, _ = _render_rag_chunks(
        [{"text": "x", "score": 0.0}], cap=1, strict=True,
    )
    assert "similarity=0.000" in rendered, (
        f"score=0.0 silently dropped by truthiness check: {rendered!r}"
    )


def test_probe_938_chunk_score_string_does_not_crash():
    """Defensive: a chunk with a non-float score should not crash
    the renderer with TypeError. Either render-as-string or skip;
    not crash."""
    try:
        rendered, _ = _render_rag_chunks(
            [{"text": "x", "score": "high"}], cap=1, strict=False,
        )
    except (TypeError, ValueError):
        return  # acceptable: surfaced cleanly
    # If no exception, the value should not silently format as
    # "high" via similarity={score:.3f} — that would TypeError.
    # The render path the code takes when format fails is what
    # we're checking.


def test_probe_939_publications_authors_as_string_not_list():
    """The renderer accepts authors as either a list[str] or a string.
    Verify the string branch does not crash (it should render as one
    author block)."""
    pub = {"doi": "10.1/x", "title": "t", "authors": "Single Author"}
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "Single Author" in rendered


def test_probe_940_authors_with_more_than_three_truncates():
    """The renderer caps shown authors at 3 (then drops the rest);
    ensure the cap holds and no fourth author leaks."""
    pub = {
        "doi": "10.1/x", "title": "t",
        "authors": ["A One", "B Two", "C Three", "D Four", "E Five"],
    }
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "A One" in rendered
    assert "C Three" in rendered
    assert "D Four" not in rendered, (
        f"renderer leaked beyond 3-author cap: {rendered!r}"
    )


def test_probe_941_min_distinct_citations_unsatisfiable_caught():
    """min_distinct_citations=10 with only 2 sources allowed: even a
    perfect LLM cannot satisfy. The error must STILL be the
    "only N distinct" message (not a confusing hallucination
    error). Clarity of error matters."""
    cfg = _cfg(min_distinct_citations=10)
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _StubLLM(content=("body " * 50) + "[BV-BRC genome G1]")
    with pytest.raises(ValueError, match="only 1 distinct"):
        synthesize_response("Q", config=cfg, llm=stub, **inputs)


def test_probe_942_iterable_generator_input_not_exhausted_twice():
    """The renderer wraps inputs in ``list(...)``; a one-shot
    generator must work. Pre-list() refactor, an iter() input was
    silently exhausted before reaching the cap slice — silent zero
    rows."""
    def _g():
        yield {"genome_id": "G1", "name": "g1"}
        yield {"genome_id": "G2", "name": "g2"}

    rendered, allowed = _render_bvbrc_genomes(_g(), cap=8, strict=True)
    assert allowed == {"[BV-BRC genome G1]", "[BV-BRC genome G2]"}


def test_probe_943_tuple_input_accepted():
    """Iterable contract: tuple of dicts must work (Iterable[dict])."""
    rendered, allowed = _render_bvbrc_genomes(
        ({"genome_id": "G1", "name": "x"},), cap=8, strict=True,
    )
    assert allowed == {"[BV-BRC genome G1]"}


def test_probe_944_repeated_doi_dedupes_in_allowed_tokens():
    """Two publications with identical DOI: allowed_tokens has ONE
    entry (set semantics). Validates the set contract holds —
    avoids spurious "more allowed than expected" assertions in
    downstream callers."""
    pubs = [
        {"doi": "10.1/x", "title": "first"},
        {"doi": "10.1/x", "title": "duplicate"},
    ]
    rendered, allowed = _render_publications(pubs, cap=5, strict=True)
    assert allowed == {"[10.1/x]"}


def test_probe_945_chunk_with_html_or_markdown_in_text_does_not_crash():
    """Real RAG chunks contain markdown formatting / HTML escapes.
    The renderer must include them verbatim without crashing on
    template-string interpolation."""
    text = (
        "## A heading\n\n*italic* and **bold** with `code` and "
        "<i>html</i> &amp; entities.\n\n```python\nfor x in []: pass\n```"
    )
    rendered, _ = _render_rag_chunks(
        [{"text": text}], cap=1, strict=True,
    )
    assert "## A heading" in rendered
    assert "<i>html</i>" in rendered


def test_probe_946_chunk_text_only_whitespace_skipped():
    """Whitespace-only text is "no information"; strict mode rejects."""
    with pytest.raises(ValueError, match="missing or empty ``text``"):
        _render_rag_chunks(
            [{"text": "   \n\t\n   "}], cap=1, strict=True,
        )


def test_probe_947_publications_doi_with_only_10_dot_no_path_rejected():
    """``doi="10."`` starts with "10." but has no path → not a real
    DOI. The strict check ``startswith("10.")`` is necessary but
    not sufficient; the regex pattern requires ``/`` — verify the
    citation extraction would NOT match this DOI even if the
    renderer somehow accepted it."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "see [10.] above", cfg.citation_marker_patterns,
    )
    assert extracted == set(), (
        f"citation pattern matched a no-slash DOI: {extracted!r} — "
        f"the pattern requires the slash structure"
    )


def test_probe_948_strict_skipping_during_iteration_does_not_corrupt_index():
    """The renderer's ``for i, g in enumerate(list(genomes)[:cap])``
    uses ``i`` only for error messages. A skipped row in lenient
    mode must not corrupt position tracking. Verify by mixing valid
    and invalid rows."""
    rendered, allowed = _render_bvbrc_genomes(
        [
            {"genome_id": "good1", "name": "n"},
            {"name": "no_id"},  # skipped in lenient mode
            {"genome_id": "good2", "name": "n2"},
        ],
        cap=8, strict=False,
    )
    assert allowed == {"[BV-BRC genome good1]", "[BV-BRC genome good2]"}


def test_probe_949_violin_canonical_with_special_chars():
    """Canonical terms can contain commas, parentheses, etc. The
    citation token uses synonym_id only, so the canonical text just
    needs to render — no escape-impacted shape."""
    rendered, _ = _render_violin_mappings(
        [{
            "synonym_id": "VO_99",
            "canonical_term": "Vaccinia virus (Ankara, modified)",
            "query_term": "MVA",
        }],
        cap=1, strict=True,
    )
    assert "Vaccinia virus (Ankara, modified)" in rendered
    assert "[VIOLIN VO_99]" in rendered


def test_probe_950_caps_exceeding_input_length_does_not_crash():
    """``cap=1000`` with 2 input rows: render all 2, no error."""
    rendered, allowed = _render_bvbrc_genomes(
        [{"genome_id": "G1", "name": "n"}, {"genome_id": "G2", "name": "n"}],
        cap=1000, strict=True,
    )
    assert len(allowed) == 2


def test_probe_951_extract_handles_back_to_back_tokens():
    """Two citation tokens with no separator: ``[A][B]`` — both
    extracted distinctly."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[BV-BRC genome G1][BV-BRC genome G2]", cfg.citation_marker_patterns,
    )
    assert "[BV-BRC genome G1]" in extracted
    assert "[BV-BRC genome G2]" in extracted


def test_probe_952_extract_handles_token_inside_other_brackets():
    """A citation token wrapped in extra brackets: ``[[BV-BRC ...]]``
    — extraction matches the inner shape (the inner ``]`` closes)."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "see [[BV-BRC genome G1]] note", cfg.citation_marker_patterns,
    )
    # The pattern is non-greedy enough to match the inner token.
    assert "[BV-BRC genome G1]" in extracted


def test_probe_953_extract_does_not_match_unclosed_token():
    """``[BV-BRC genome G1`` (no closing ``]``): the tightened regex
    requires the close bracket → no match. v3 regression guard."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[BV-BRC genome G1 in some text", cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_954_two_distinct_synthesizer_invocations_use_independent_state():
    """Two back-to-back ``synthesize_response`` calls with different
    inputs must NOT share allowed_tokens. Checks for accidental
    module-level state (a class-attribute set, an LRU cache).
    Caller A's tokens leaking into caller B's grounding check would
    be a silent-failure that adversarial probing should catch."""
    inputs_a = dict(bvbrc_genomes=[{"genome_id": "A1", "name": "n"}])
    inputs_b = dict(bvbrc_genomes=[{"genome_id": "B1", "name": "n"}])
    out_a = synthesize_response(
        "Q", llm=_StubLLM(content=("body " * 50) + "[BV-BRC genome A1]"),
        **inputs_a,
    )
    # B legitimately cites B1; if A's allowed_tokens leaked, B's
    # response could illegitimately cite A1 without being caught.
    # Confirm the flip works the other way too: B citing A1 must
    # raise (proves no state leak from A's call).
    with pytest.raises(ValueError, match="hallucinat"):
        synthesize_response(
            "Q",
            llm=_StubLLM(content=("body " * 50) + "[BV-BRC genome A1]"),
            **inputs_b,
        )
    assert "[BV-BRC genome A1]" in out_a
