"""Probe batch 41 — adversarial probes against citation-extraction
and grounding-validator interactions.

Streak before this batch: 24/300 post-AQ (reset by probe 1066).
Probe naming: 1080–1104.

Distinct probes only. Post-1066 the regex was tightened; this batch
exercises shapes the new regex now handles.
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
)


pytestmark = pytest.mark.integration


def _cfg(**overrides) -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)


class _Stub:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, msgs):
        return AIMessage(content=self.content)


# --------------------------------------------------------------------------- #
# Probes 1080–1104
# --------------------------------------------------------------------------- #


def test_probe_1080_extract_two_doi_separated_by_text_distinctly_extracts_both():
    """Post-fix-1066: ``[10.1/x] some text [10.2/y]`` -> two distinct
    tokens. The greedy-match-across-tokens shape no longer matches
    the bigger span."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "see [10.1/x] some text [10.2/y]", cfg.citation_marker_patterns,
    )
    assert extracted == {"[10.1/x]", "[10.2/y]"}


def test_probe_1081_extract_truncated_then_full_does_not_swallow():
    """The exact bug-1066 shape: an interrupted earlier citation
    followed by a complete one. Post-fix, the truncated form does
    NOT match (no closing bracket within the allowed character class)."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "see [10.1234/abc... and also [10.5/y]", cfg.citation_marker_patterns,
    )
    assert extracted == {"[10.5/y]"}, (
        f"post-fix expected only [10.5/y]; got {extracted!r}"
    )


def test_probe_1082_extract_doi_followed_immediately_by_doi_no_space():
    """Two DOIs back-to-back with no separator: ``[10.1/x][10.2/y]``.
    Post-fix the two tokens are distinct."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1/x][10.2/y]", cfg.citation_marker_patterns,
    )
    assert extracted == {"[10.1/x]", "[10.2/y]"}


def test_probe_1083_extract_violin_followed_by_doi_not_swallowed():
    """A truncated VIOLIN ``[VIOLIN VO...`` followed by a complete
    DOI must NOT cause the VIOLIN match to span into the DOI."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[VIOLIN VO_INCOMPLETE_ followed by [10.1/y]",
        cfg.citation_marker_patterns,
    )
    # The VIOLIN pattern won't match (whitespace inside breaks now).
    # The DOI pattern matches the complete second token.
    assert "[10.1/y]" in extracted
    # The VIOLIN attempt does NOT produce a token spanning the DOI.
    assert all("VO_INCOMPLETE" not in t for t in extracted)


def test_probe_1084_extract_doi_with_space_after_open_does_not_match():
    """Old shape: ``[10.1234/abc...`` followed by space and another
    citation could match. Now, since the inner class excludes whitespace,
    the regex won't even match a DOI that contains a space inside."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1234/has space inside]", cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_1085_grounding_with_legitimate_doi_followed_by_truncated_one():
    """End-to-end: an LLM that cites a real DOI then starts but
    doesn't finish a second citation — the legitimate one is
    preserved, the truncated form is not extracted (no false-
    positive citation token, no spurious grounding violation)."""
    cfg = _cfg()
    pubs = [{"doi": "10.1/real", "title": "T"}]
    body = (
        "body " * 50 + "Cite this: [10.1/real]. "
        "I started [10.2/incomplete..."
    )
    stub = _Stub(content=body)
    out = synthesize_response("Q", llm=stub, publications=pubs, config=cfg)
    assert "[10.1/real]" in out


def test_probe_1086_extract_does_not_match_doi_with_internal_open_bracket():
    """A DOI containing an unescaped ``[`` (synthetic adversarial
    case) should NOT match — the inner class now excludes ``[``."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1/has[bracket/x]", cfg.citation_marker_patterns,
    )
    # No match — the ``[`` would terminate the inner class, but the
    # outer ``\]`` requires a literal close. A single token cannot
    # match.
    assert extracted == set()


def test_probe_1087_render_bvbrc_with_id_containing_space_renders_but_LLM_cannot_cite():
    """A BV-BRC genome ID with embedded space (sloppy data) renders
    as ``[BV-BRC genome G 1]`` in the prompt. But the regex CANNOT
    match this token because the inner class excludes whitespace.
    So the LLM citing it would fail the EXTRACTION step entirely
    (no token extracted), and grounding's
    ``min_distinct_citations`` would catch the lack of valid
    citations.

    This is a defensible failure mode: the renderer renders sloppy
    data, but the validator rejects the LLM's response (because no
    valid citation could be extracted). Operators see "0 distinct
    citations" rather than a confusing hallucination message —
    pointing at the data quality issue."""
    rendered, allowed = _render_bvbrc_genomes(
        [{"genome_id": "G 1", "name": "n"}], cap=1, strict=True,
    )
    # Renderer happily produces the citation token text.
    assert allowed == {"[BV-BRC genome G 1]"}
    # But the EXTRACTION pattern won't recover it from LLM output.
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[BV-BRC genome G 1]", cfg.citation_marker_patterns,
    )
    assert extracted == set(), (
        f"unexpected extraction: {extracted!r}; sloppy IDs with "
        f"whitespace must NOT extract back"
    )


def test_probe_1088_extract_handles_no_brackets_at_all():
    """A response with no brackets at all — extraction returns empty."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "no citations whatsoever in this text", cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_1089_extract_handles_only_open_brackets():
    """Open-bracket-only text — no extraction matches without close."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10. [VIOLIN [BV-BRC genome [RAG chunk",
        cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_1090_extract_handles_only_close_brackets():
    """Close-bracket-only text — no opens, no extraction."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "10.x] VIOLIN VO_1] BV-BRC genome G1] RAG chunk #1]",
        cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_1091_synthesize_with_min_response_chars_zero_skips_curtailed_check():
    """Disabling the curtailed-response check (``min_response_chars=0``)
    must let very short responses pass — IF they still satisfy
    citation rules. Tests that the gate is fully removable."""
    cfg = _cfg(min_response_chars=0)
    stub = _Stub(content="[BV-BRC genome G1]")
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    out = synthesize_response("Q", llm=stub, config=cfg, **inputs)
    assert out == "[BV-BRC genome G1]"


def test_probe_1092_grounding_with_extracted_token_exactly_matching_input():
    """Sanity check: the EXACT token the renderer authorizes is the
    token grounding accepts. If a future refactor changes the
    renderer's f-string format (e.g. ``[BV-BRC GENOME G1]`` upper
    case), grounding would silently break. Pin via end-to-end."""
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content="body " * 50 + "[BV-BRC genome G1]")
    out = synthesize_response("Q", llm=stub, **inputs)
    assert "[BV-BRC genome G1]" in out


def test_probe_1093_extract_pattern_compiles_without_runtime_warning():
    """All four default patterns must compile cleanly. A regex
    warning at import would silently degrade extraction; verify
    no warnings fire."""
    import warnings
    cfg = _cfg()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for pat in cfg.citation_marker_patterns:
            re.compile(pat)


def test_probe_1094_extract_with_unicode_in_id_body():
    """The new pattern excludes whitespace + brackets, but still
    accepts arbitrary unicode chars (Greek, CJK). Verify."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[VIOLIN VO_β-1]", cfg.citation_marker_patterns,
    )
    assert "[VIOLIN VO_β-1]" in extracted


def test_probe_1095_extract_does_not_match_across_newlines():
    """An interrupted citation followed by a newline followed by
    another text — the regex's character class excludes whitespace,
    so newlines also break the match."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1/abc\n more text [10.2/y]", cfg.citation_marker_patterns,
    )
    # Only the second, fully-formed one matches.
    assert extracted == {"[10.2/y]"}


def test_probe_1096_extract_does_not_match_across_tabs():
    """Tabs are whitespace; same story as newlines."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1/abc\t [10.2/y]", cfg.citation_marker_patterns,
    )
    assert extracted == {"[10.2/y]"}


def test_probe_1097_synthesize_response_with_publications_dois_with_only_digits_and_dot():
    """A DOI like ``10.1234567890.12345/x`` (multi-dot prefix) — the
    pattern is ``\\[10\\.[0-9]+/...`` which requires DIGITS only between
    ``10.`` and ``/``. Multi-dot prefixes won't match."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1234567890.12345/x]", cfg.citation_marker_patterns,
    )
    # The pattern requires ``[0-9]+/`` — a dot in the prefix breaks.
    assert extracted == set()


def test_probe_1098_render_publication_doi_with_dot_in_prefix_passes_strict_check():
    """The renderer's strict check is ``startswith("10.")``. A DOI
    like ``10.1234.5/x`` passes the strict check (starts with ``10.``)
    but the citation pattern won't extract it. Pin: this is a
    silent-failure-resistant shape — the renderer accepts but the
    grounding validator rejects (because extraction is empty)."""
    pub = {"doi": "10.1234.5/x", "title": "T"}
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    # Renderer accepts.
    assert "[10.1234.5/x]" in allowed
    # Extraction won't recover it from LLM output.
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[10.1234.5/x]", cfg.citation_marker_patterns,
    )
    assert extracted == set()


def test_probe_1099_grounding_when_renderer_authorizes_pattern_extraction_cannot_match():
    """End-to-end test of the silent-failure-resistance from
    1087/1098: when the renderer authorizes a token that extraction
    can't recover (e.g. DOI with dot in prefix), grounding never
    sees a match → min_distinct_citations gate fires with a clear
    error. NOT a confusing "hallucination" message."""
    cfg = _cfg()
    inputs = dict(publications=[{"doi": "10.1234.5/x", "title": "T"}])
    stub = _Stub(content="body " * 50 + "[10.1234.5/x]")
    with pytest.raises(ValueError, match="distinct citation"):
        synthesize_response("Q", llm=stub, config=cfg, **inputs)


def test_probe_1100_extract_finds_multiple_violin_tokens():
    """Three VIOLIN citations in a row — distinct extraction sees all."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[VIOLIN VO_1] and [VIOLIN VO_2] and [VIOLIN VO_3]",
        cfg.citation_marker_patterns,
    )
    assert extracted == {"[VIOLIN VO_1]", "[VIOLIN VO_2]", "[VIOLIN VO_3]"}


def test_probe_1101_extract_returns_set_not_list():
    """Caller may rely on set semantics (membership tests).
    Pin the return type."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[RAG chunk #1]", cfg.citation_marker_patterns,
    )
    assert isinstance(extracted, set)


def test_probe_1102_synthesize_with_validate_off_accepts_uncited_token_in_text():
    """``validate_citations_against_inputs=False`` AND
    ``min_distinct_citations`` >0: the LLM may cite anything that
    matches the regex; grounding is off, distinct count enforced.
    Verify."""
    cfg = _cfg(
        validate_citations_against_inputs=False,
        min_distinct_citations=1,
    )
    inputs = dict(bvbrc_genomes=[{"genome_id": "REAL", "name": "n"}])
    # LLM cites a fake VIOLIN that's not in inputs; grounding off,
    # distinct count =1 satisfied.
    stub = _Stub(content="body " * 50 + "[VIOLIN VO_FAKE]")
    out = synthesize_response("Q", llm=stub, config=cfg, **inputs)
    assert "[VIOLIN VO_FAKE]" in out


def test_probe_1103_render_with_no_text_field_chunk_lenient_does_not_corrupt_count():
    """Lenient mode with several missing-text chunks: surviving
    count must be the COUNT OF VALID CHUNKS, not the input length.
    Pin so a future refactor that increments surviving by 1 per
    iteration breaks this test."""
    rendered, allowed = _render_publications([], cap=5, strict=False)
    assert allowed == set()


def test_probe_1104_synthesize_response_does_not_invoke_llm_when_input_query_invalid():
    """Pre-LLM contract: invalid query (empty / None) must reject
    BEFORE the LLM is called. Verify by counting LLM invocations."""
    invocations = []

    class _CountingLLM:
        def invoke(self, msgs):
            invocations.append(msgs)
            return AIMessage(content="should never happen")

    with pytest.raises(ValueError, match="non-empty string"):
        synthesize_response("", llm=_CountingLLM())
    assert invocations == [], (
        "LLM was called for an empty query — pre-LLM gate failed"
    )
