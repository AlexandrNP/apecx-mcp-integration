"""Unit tests for ``apecx_integration.agents.rag_synthesis``.

Mock-policy compliance (CLAUDE.md unit-mock / integration-test parity):
    All tests in this file use a stub LLM (a class with a synchronous
    ``invoke()`` method that returns a canned ``AIMessage``). The
    matching integration test that exercises the same flow against a
    real local LLM (Ollama / mistral-nemo) is
    ``tests/integration/test_rag_synthesis_against_ollama.py``. If
    that integration test goes missing, this unit suite is mock-only
    coverage and the workspace policy says it's not enough on its
    own.

Renderers are tested indirectly through ``synthesize_response`` (the
public API) AND directly via the underscored helpers — the rendering
contract is load-bearing for adversarial robustness, so we assert on
its shape directly to catch regressions that an end-to-end test
might mask.
"""

from __future__ import annotations

import re
from typing import Any

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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _StubLLM:
    """Minimal stub: ``invoke(messages)`` returns a canned AIMessage.

    The real ``ChatOpenAI`` client returns a ``BaseMessage`` whose
    ``.content`` is the model's text. Tests that need to inspect the
    prompt the synthesizer assembled can check ``self.received``.
    """

    def __init__(self, content: str = "REPLACED_BY_TEST") -> None:
        self.content = content
        self.received: list[Any] = []

    def invoke(self, messages: Any) -> AIMessage:
        self.received.append(messages)
        return AIMessage(content=self.content)


def _good_response() -> str:
    """A response that satisfies every default validation knob.

    Length > 200 chars; cites every default source; uses the closed-
    bracket citation shape the tightened patterns require.
    """
    return (
        "Sindbis virus belongs to the *Togaviridae* family of "
        "alphaviruses. The reference genome [BV-BRC genome 11036.7] "
        "captures the canonical strain. Recent semantic chunks "
        "describe envelope glycoprotein structure [RAG chunk #1] and "
        "polyprotein cleavage steps [RAG chunk #2]. The VIOLIN cached "
        "synonym [VIOLIN VO_0000001] resolves the colloquial name. "
        "Structural work in [10.1234/abc] confirms the receptor "
        "binding site geometry."
    )


def _full_inputs() -> dict[str, list[dict[str, Any]]]:
    """A complete, validation-clean retrieval bundle."""
    return {
        "rag_chunks": [
            {"text": "Sindbis envelope glycoprotein E1/E2 forms heterodimers."},
            {"text": "Polyprotein cleavage proceeds via nsP2 protease."},
        ],
        "bvbrc_genomes": [
            {"genome_id": "11036.7", "genome_name": "Sindbis virus AR339"},
        ],
        "violin_mappings": [
            {"synonym_id": "VO_0000001",
             "query_term": "sindbis", "canonical_term": "Sindbis virus"},
        ],
        "publications": [
            {"doi": "10.1234/abc", "title": "Sindbis envelope structure"},
        ],
    }


# --------------------------------------------------------------------------- #
# Default config / module-level
# --------------------------------------------------------------------------- #


def test_default_config_path_exists():
    assert DEFAULT_SYNTHESIS_CONFIG_PATH.is_file()


def test_default_config_loads_with_expected_defaults():
    """The bundled YAML must validate against the Pydantic schema and
    carry the documented defaults. If a future commit drifts the YAML
    out of sync with the schema, this catches it before runtime."""
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    cfg = SynthesisConfig.model_validate(raw)
    assert cfg.require_inline_citations is True
    assert cfg.min_distinct_citations == 1
    assert cfg.min_response_chars == 200
    assert cfg.fail_on_empty_retrieval is True
    assert cfg.strict_input_validation is True
    assert cfg.max_rag_chunks == 8
    # Tightened patterns: each must match a CLOSED token shape.
    for pat in cfg.citation_marker_patterns:
        # Every pattern ends with `\]` (escaped close-bracket) — that
        # is what makes them full-token, not prefix-only.
        assert pat.endswith(r"\]"), pat


# --------------------------------------------------------------------------- #
# Query validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "   ", "\n\t  "])
def test_empty_query_rejected(bad):
    with pytest.raises(ValueError, match="non-empty string"):
        synthesize_response(bad, llm=_StubLLM())


@pytest.mark.parametrize("bad", [None, 42, [], {"q": "hi"}])
def test_non_string_query_rejected(bad):
    with pytest.raises(ValueError, match="non-empty string"):
        synthesize_response(bad, llm=_StubLLM())


# --------------------------------------------------------------------------- #
# Empty-retrieval policy
# --------------------------------------------------------------------------- #


def test_all_empty_retrieval_fails_pre_llm_with_default_config():
    """fail_on_empty_retrieval=True (default) rejects before any LLM
    invocation. The stub LLM raises if reached."""

    class _NoCallLLM:
        def invoke(self, _msgs):
            raise AssertionError("LLM must not be called when retrieval is empty")

    with pytest.raises(ValueError, match="every retrieval input is empty"):
        synthesize_response("Sindbis virus?", llm=_NoCallLLM())


def test_all_empty_retrieval_allowed_when_disabled():
    """Operators can opt out of the pre-LLM check; downstream
    citation validation still kicks in (and rejects the citation-free
    response from the stub)."""
    cfg = _cfg(fail_on_empty_retrieval=False, min_response_chars=0)
    stub = _StubLLM(content="No citation marker here at all.")
    with pytest.raises(ValueError, match="distinct citation token"):
        synthesize_response("Sindbis?", llm=stub, config=cfg)


def test_partial_retrieval_passes_empty_check():
    """Any one source populated is enough to pass the empty-retrieval
    gate. The synth must still return Markdown with citations."""
    inputs = {"bvbrc_genomes": [{"genome_id": "11036.7", "name": "X"}]}
    stub = _StubLLM(content=_good_response())
    out = synthesize_response("Sindbis?", llm=stub, **inputs)
    assert out == _good_response()


# --------------------------------------------------------------------------- #
# Strict input validation per row
# --------------------------------------------------------------------------- #


def test_strict_rejects_bvbrc_row_with_no_id():
    with pytest.raises(ValueError, match="missing ``genome_id``"):
        synthesize_response(
            "Q", bvbrc_genomes=[{"name": "Sindbis"}], llm=_StubLLM(),
        )


def test_strict_rejects_violin_row_with_no_synonym_id():
    with pytest.raises(ValueError, match="missing ``synonym_id``"):
        synthesize_response(
            "Q", violin_mappings=[{"canonical_term": "Sindbis"}], llm=_StubLLM(),
        )


def test_strict_rejects_violin_row_with_no_canonical_term():
    with pytest.raises(ValueError, match="missing ``canonical_term``"):
        synthesize_response(
            "Q", violin_mappings=[{"synonym_id": "VO_0000001"}], llm=_StubLLM(),
        )


def test_strict_rejects_pub_with_no_doi():
    with pytest.raises(ValueError, match="missing or non-DOI"):
        synthesize_response(
            "Q", publications=[{"title": "Untitled"}], llm=_StubLLM(),
        )


def test_strict_rejects_pub_with_non_doi_string():
    """``doi="abc"`` does not start with ``10.`` — it's not a DOI literal.
    The validator must reject so the citation pattern can't match a
    confabulated marker downstream."""
    with pytest.raises(ValueError, match="missing or non-DOI"):
        synthesize_response(
            "Q", publications=[{"doi": "abc"}], llm=_StubLLM(),
        )


def test_strict_rejects_chunk_with_no_text():
    with pytest.raises(ValueError, match="missing or empty ``text``"):
        synthesize_response(
            "Q", rag_chunks=[{"id": "c1"}], llm=_StubLLM(),
        )


def test_strict_rejects_non_dict_row():
    """Caller passes a string in place of a dict. Pre-2026-04-27 this
    would crash with AttributeError later; now it raises a typed
    ValueError naming the row index."""
    with pytest.raises(ValueError, match="expected dict"):
        synthesize_response(
            "Q", bvbrc_genomes=["a string, not a dict"], llm=_StubLLM(),
        )


def test_lenient_skips_rows_with_warning(caplog):
    """strict_input_validation=False: bad rows skipped with logger.warning
    rather than raised. With at least one good row remaining, synthesis
    proceeds normally."""
    caplog.set_level("WARNING", logger="apecx_integration.agents.rag_synthesis.synthesizer")
    cfg = _cfg(strict_input_validation=False, min_response_chars=0)
    stub = _StubLLM(content=_good_response())
    out = synthesize_response(
        "Q",
        bvbrc_genomes=[
            {"name": "no id row"},
            {"genome_id": "11036.7", "name": "good row"},
        ],
        llm=stub, config=cfg,
    )
    assert out == _good_response()
    skip_logs = [r for r in caplog.records if "skipping" in r.message]
    assert any("bvbrc_genome" in r.message for r in skip_logs)


def test_lenient_all_rows_skipped_then_empty_check_fires():
    """When lenient mode skips every row in every source, the all-empty
    check should still fire (it counts SURVIVING rows, not input rows)."""
    cfg = _cfg(strict_input_validation=False)
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        synthesize_response(
            "Q",
            bvbrc_genomes=[{"name": "no id"}],
            violin_mappings=[{"canonical_term": "no synonym_id"}],
            llm=_StubLLM(),
            config=cfg,
        )


# --------------------------------------------------------------------------- #
# LLM response validation
# --------------------------------------------------------------------------- #


def test_empty_llm_content_rejected():
    stub = _StubLLM(content="")
    inputs = _full_inputs()
    with pytest.raises(ValueError, match="empty/non-string"):
        synthesize_response("Q", llm=stub, **inputs)


def test_whitespace_only_llm_content_rejected():
    stub = _StubLLM(content="   \n\t   ")
    inputs = _full_inputs()
    with pytest.raises(ValueError, match="empty/non-string"):
        synthesize_response("Q", llm=stub, **inputs)


def test_curtailed_response_rejected():
    stub = _StubLLM(content="OK [RAG chunk #1].")
    inputs = _full_inputs()
    with pytest.raises(ValueError, match="curtailed"):
        synthesize_response("Q", llm=stub, **inputs)


def test_response_with_no_citations_rejected():
    stub = _StubLLM(content="Some long but uncited text. " * 30)
    inputs = _full_inputs()
    with pytest.raises(ValueError, match="distinct citation token"):
        synthesize_response("Q", llm=stub, **inputs)


def test_mono_citation_rejected_when_min_distinct_2():
    stub = _StubLLM(
        content=("Long enough body of text. " * 20) + "[RAG chunk #1]"
    )
    inputs = _full_inputs()
    cfg = _cfg(min_distinct_citations=2)
    with pytest.raises(ValueError, match="only 1 distinct citation"):
        synthesize_response("Q", llm=stub, config=cfg, **inputs)


def test_happy_path_returns_llm_content_verbatim():
    stub = _StubLLM(content=_good_response())
    inputs = _full_inputs()
    out = synthesize_response("Q", llm=stub, **inputs)
    assert out == _good_response()


def test_repeated_citation_counts_as_one_distinct():
    """The LLM cites the same source 5x. With min_distinct=2 this is
    still 1 distinct → reject. Verifies the dedupe semantics."""
    stub = _StubLLM(
        content=("Lots of text. " * 30) + "[RAG chunk #1] " * 5
    )
    inputs = _full_inputs()
    cfg = _cfg(min_distinct_citations=2)
    with pytest.raises(ValueError, match="only 1 distinct"):
        synthesize_response("Q", llm=stub, config=cfg, **inputs)


def test_malformed_marker_does_not_count():
    """``[BV-BRC genome ?]`` matches the closed pattern but is data
    garbage. Strict input validation prevents the row from being
    rendered in the first place — but if the LLM emits the marker on
    its own, the validator still counts it. Validate the actual
    contract: distinct match counting is purely string-based.

    This is a test of CURRENT behavior (validator does not deep-check
    that the cited ID was actually in the inputs). A future probe
    could promote that to a real check."""
    stub = _StubLLM(
        content=("Lots of text. " * 30) + "[BV-BRC genome ?]"
    )
    inputs = _full_inputs()
    # Default min_distinct=1 → the malformed-but-syntactically-valid
    # marker satisfies the rule. Document that this passes here so a
    # future tightening test fails loudly when we change the contract.
    out = synthesize_response("Q", llm=stub, **inputs)
    assert "[BV-BRC genome ?]" in out


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #


def test_caps_applied_to_each_source():
    """``max_*`` caps clip input lists to the configured ceiling."""
    cfg = _cfg(
        max_rag_chunks=2, max_bvbrc_genomes=1,
        max_violin_mappings=1, max_publications=1,
        # Disable post-LLM gates so the test focuses on caps.
        min_response_chars=0, require_inline_citations=False,
        fail_on_empty_retrieval=False,
    )
    stub = _StubLLM(content="x")
    chunks = [{"text": f"chunk {i}"} for i in range(10)]
    genomes = [{"genome_id": f"id{i}", "name": f"g{i}"} for i in range(5)]
    mappings = [
        {"synonym_id": f"vo{i}", "canonical_term": f"c{i}"} for i in range(5)
    ]
    pubs = [{"doi": f"10.1234/{i}", "title": f"p{i}"} for i in range(5)]
    synthesize_response(
        "Q",
        rag_chunks=chunks, bvbrc_genomes=genomes,
        violin_mappings=mappings, publications=pubs,
        llm=stub, config=cfg,
    )
    # Inspect the prompt the synthesizer sent.
    sent_user_msg = stub.received[0][1].content
    # Caps mean only N of each source surface — verify by counting
    # the distinguishing token of each renderer.
    assert sent_user_msg.count("### RAG chunk #") == 2
    # bvbrc — `**BV-BRC genome `
    assert sent_user_msg.count("**BV-BRC genome `") == 1
    # violin — `[VIOLIN ` markers in the prompt itself (one per row)
    assert sent_user_msg.count("[VIOLIN vo0]") == 1
    # publications — `**[10.` prefix
    assert sent_user_msg.count("**[10.1234/0]") == 1


# --------------------------------------------------------------------------- #
# Renderers (direct unit tests)
# --------------------------------------------------------------------------- #


def test_render_rag_chunks_numbers_by_surviving_position():
    """Skipped rows must NOT shift downstream chunk numbers — chunk #1
    is always the first surviving chunk."""
    rendered, count = _render_rag_chunks(
        [{"id": "c0"}, {"text": "real"}, {"id": "c2"}, {"text": "real2"}],
        cap=8, strict=False,
    )
    assert count == 2
    assert "### RAG chunk #1" in rendered
    assert "### RAG chunk #2" in rendered
    assert "### RAG chunk #3" not in rendered


def test_render_rag_chunks_includes_optional_metadata():
    rendered, _ = _render_rag_chunks(
        [{"text": "x", "id": "c1", "source": "Pubmed/123", "score": 0.876}],
        cap=8, strict=True,
    )
    assert "id=c1" in rendered
    assert "source=Pubmed/123" in rendered
    assert "similarity=0.876" in rendered


def test_render_bvbrc_falls_back_on_alt_keys():
    """The renderer accepts both ``genome_id``/``id`` and
    ``genome_name``/``name`` shapes — the harvester step might emit
    either."""
    rendered, count = _render_bvbrc_genomes(
        [{"id": "11036.7", "name": "Sindbis"}], cap=5, strict=True,
    )
    assert count == 1
    assert "BV-BRC genome `11036.7`" in rendered
    assert "Sindbis" in rendered


def test_render_violin_emits_citation_marker():
    rendered, _ = _render_violin_mappings(
        [{"synonym_id": "VO_0000001",
          "canonical_term": "Sindbis virus", "query_term": "sindbis"}],
        cap=5, strict=True,
    )
    # The token is what the LLM is supposed to copy into its citation.
    assert "[VIOLIN VO_0000001]" in rendered


def test_render_publications_truncates_long_abstract_with_ellipsis():
    long_abstract = "A" * 500
    rendered, _ = _render_publications(
        [{"doi": "10.1234/abc", "title": "T", "abstract": long_abstract}],
        cap=1, strict=True,
    )
    # 300 chars + ellipsis.
    assert ("A" * 300 + "…") in rendered
    assert "A" * 301 not in rendered


def test_render_handles_empty_input_gracefully():
    """Each renderer returns a `(no X)` placeholder on empty input.
    The placeholder communicates to the LLM that the source is
    deliberately empty (vs. forgotten), which dampens confabulation."""
    for fn, kind in [
        (_render_rag_chunks, "no RAG chunks"),
        (_render_bvbrc_genomes, "no BV-BRC"),
        (_render_violin_mappings, "no VIOLIN"),
        (_render_publications, "no publications"),
    ]:
        rendered, count = fn([], cap=5, strict=True)
        assert count == 0
        assert kind in rendered


# --------------------------------------------------------------------------- #
# Distinct citation extraction
# --------------------------------------------------------------------------- #


def test_extract_distinct_citations_dedupes():
    txt = "[RAG chunk #1] [RAG chunk #1] [RAG chunk #2] [VIOLIN VO_1]"
    cfg = SynthesisConfig.model_validate(
        {"system_prompt": "x"}  # other fields use defaults
    )
    out = _extract_distinct_citations(txt, cfg.citation_marker_patterns)
    assert out == {"[RAG chunk #1]", "[RAG chunk #2]", "[VIOLIN VO_1]"}


def test_extract_distinct_citations_handles_doi_with_punctuation():
    """DOIs can contain ``/``, ``.``, ``-`` etc. The pattern's
    character class must accept them."""
    txt = "Cited [10.1038/s41586-023-12345-6] and [10.1101/2024.01.02.x]"
    cfg = SynthesisConfig.model_validate({"system_prompt": "x"})
    out = _extract_distinct_citations(txt, cfg.citation_marker_patterns)
    assert "[10.1038/s41586-023-12345-6]" in out
    assert "[10.1101/2024.01.02.x]" in out


def test_extract_distinct_citations_no_match_returns_empty_set():
    txt = "no citations here"
    cfg = SynthesisConfig.model_validate({"system_prompt": "x"})
    out = _extract_distinct_citations(txt, cfg.citation_marker_patterns)
    assert out == set()


# --------------------------------------------------------------------------- #
# Helpers (test-internal)
# --------------------------------------------------------------------------- #


def _cfg(**overrides: Any) -> SynthesisConfig:
    """Load default config + apply overrides. Used by tests that need
    to relax one specific gate without re-typing every default field."""
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)
