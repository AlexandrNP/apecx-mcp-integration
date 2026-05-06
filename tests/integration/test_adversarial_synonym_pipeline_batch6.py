"""Adversarial probes batch 6 — probes 151-180.

Targets: citation extraction internals (_extract_distinct_citations patterns),
renderer output contracts (_render_rag_chunks, _render_bvbrc_genomes,
_render_violin_mappings, _render_publications), and
synthesize_response query validation edge cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract(text: str) -> set[str]:
    """Call _extract_distinct_citations with default patterns."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        SynthesisConfig,
        _extract_distinct_citations,
    )

    cfg = SynthesisConfig(system_prompt="test", min_response_chars=0)
    return _extract_distinct_citations(text, cfg.citation_marker_patterns)


def _render_rag(chunks, cap=10, strict=True):
    from apecx_integration.agents.rag_synthesis.synthesizer import _render_rag_chunks

    return _render_rag_chunks(chunks, cap, strict=strict)


def _render_bvbrc(genomes, cap=10, strict=True):
    from apecx_integration.agents.rag_synthesis.synthesizer import _render_bvbrc_genomes

    return _render_bvbrc_genomes(genomes, cap, strict=strict)


def _render_violin(mappings, cap=10, strict=True):
    from apecx_integration.agents.rag_synthesis.synthesizer import _render_violin_mappings

    return _render_violin_mappings(mappings, cap, strict=strict)


def _render_pubs(pubs, cap=10, strict=True):
    from apecx_integration.agents.rag_synthesis.synthesizer import _render_publications

    return _render_publications(pubs, cap, strict=strict)


# ---------------------------------------------------------------------------
# _extract_distinct_citations patterns (151-165)
# ---------------------------------------------------------------------------


def test_probe_151_extract_rag_chunk_pattern_matches():
    """[RAG chunk #N] is captured by the RAG chunk pattern."""
    result = _extract("See [RAG chunk #1] and [RAG chunk #5].")
    assert "[RAG chunk #1]" in result
    assert "[RAG chunk #5]" in result


def test_probe_152_extract_rag_chunk_pattern_non_digit_no_match():
    """[RAG chunk #X] with non-digit N does NOT match the RAG pattern."""
    result = _extract("See [RAG chunk #abc].")
    assert len(result) == 0


def test_probe_153_extract_bvbrc_pattern_matches():
    """[BV-BRC genome <id>] is captured."""
    result = _extract("Genome [BV-BRC genome 1229193.6] was sequenced.")
    assert "[BV-BRC genome 1229193.6]" in result


def test_probe_154_extract_bvbrc_pattern_whitespace_inside_id_no_match():
    """[BV-BRC genome ID WITH SPACE] does NOT match (space excluded)."""
    result = _extract("Genome [BV-BRC genome id with space].")
    assert not any("BV-BRC" in t for t in result)


def test_probe_155_extract_violin_pattern_matches():
    """[VIOLIN VOC-0012] is captured."""
    result = _extract("Vaccine [VIOLIN VOC-0012] was studied.")
    assert "[VIOLIN VOC-0012]" in result


def test_probe_156_extract_violin_pattern_bracket_inside_id_no_match():
    """[VIOLIN id[broken]] does NOT match (bracket excluded from inner class)."""
    result = _extract("Vaccine [VIOLIN id[broken]].")
    assert not any("VIOLIN" in t for t in result)


def test_probe_157_extract_doi_pattern_matches():
    """[10.1056/NEJMoa1234] is captured by the DOI pattern."""
    result = _extract("Paper [10.1056/NEJMoa1234] showed results.")
    assert "[10.1056/NEJMoa1234]" in result


def test_probe_158_extract_doi_pattern_no_prefix_no_match():
    """[5.1056/NEJMoa1234] does NOT match (DOI must start with 10.)."""
    result = _extract("Paper [5.1056/NEJMoa1234].")
    assert len(result) == 0


def test_probe_159_extract_deduplicates_repeated_same_token():
    """The same citation token repeated N times counts as 1 distinct."""
    result = _extract("[RAG chunk #1] and [RAG chunk #1] and [RAG chunk #1].")
    assert result == {"[RAG chunk #1]"}


def test_probe_160_extract_empty_text_returns_empty_set():
    """Empty input text returns empty set."""
    assert _extract("") == set()


def test_probe_161_extract_no_brackets_returns_empty_set():
    """Text with no brackets at all returns empty set."""
    assert _extract("No citations here at all, just prose.") == set()


def test_probe_162_extract_multiple_pattern_types_in_one_text():
    """All four pattern types can appear in the same text."""
    text = "[RAG chunk #1] plus [BV-BRC genome GEN-1] plus [VIOLIN VOC-1] plus [10.1234/doi.1]."
    result = _extract(text)
    assert len(result) == 4


def test_probe_163_extract_doi_with_subpath_matches():
    """DOI with long subpath like 10.1038/nature/2024.001 is captured."""
    result = _extract("See [10.1038/nature/2024.001] for details.")
    assert "[10.1038/nature/2024.001]" in result


def test_probe_164_extract_citation_adjacent_to_punctuation():
    """Citation immediately followed by period/comma is captured correctly."""
    result = _extract("Data [RAG chunk #2], and more.")
    assert "[RAG chunk #2]" in result


def test_probe_165_extract_pattern_does_not_match_plain_brackets():
    """Plain [text] without a valid prefix is NOT captured."""
    result = _extract("See [Table 1] and [Figure 2] for context.")
    assert len(result) == 0


# ---------------------------------------------------------------------------
# _render_rag_chunks output contracts (166-170)
# ---------------------------------------------------------------------------


def test_probe_166_render_rag_chunks_returns_allowed_token_set():
    """render_rag_chunks returns allowed tokens matching [RAG chunk #N] format."""
    _, allowed = _render_rag([{"text": "data", "id": "c1"}])
    assert "[RAG chunk #1]" in allowed


def test_probe_167_render_rag_chunks_empty_input_returns_no_allowed_tokens():
    """Empty chunks list: allowed_tokens is empty set."""
    _, allowed = _render_rag([])
    assert len(allowed) == 0


def test_probe_168_render_rag_chunks_tokens_are_sequential():
    """Two chunks produce [RAG chunk #1] and [RAG chunk #2], not other numbers."""
    chunks = [{"text": "first"}, {"text": "second"}]
    _, allowed = _render_rag(chunks)
    assert allowed == {"[RAG chunk #1]", "[RAG chunk #2]"}


def test_probe_169_render_rag_chunks_skipped_empty_text_renumbers():
    """Chunks with empty text are skipped; remaining chunks are renumbered."""
    chunks = [
        {"text": ""},  # skipped in lenient mode
        {"text": "valid"},
    ]
    _, allowed = _render_rag(chunks, strict=False)
    # Only 1 surviving chunk → token is #1, not #2
    assert "[RAG chunk #1]" in allowed
    assert "[RAG chunk #2]" not in allowed


def test_probe_170_render_rag_chunks_cap_limits_output():
    """cap=1 means only the first chunk produces a token."""
    chunks = [{"text": "first"}, {"text": "second"}]
    _, allowed = _render_rag(chunks, cap=1)
    assert allowed == {"[RAG chunk #1]"}


# ---------------------------------------------------------------------------
# _render_bvbrc_genomes output contracts (171-175)
# ---------------------------------------------------------------------------


def test_probe_171_render_bvbrc_genome_id_in_allowed_tokens():
    """genome_id value appears in the allowed token [BV-BRC genome <id>]."""
    genome = {"genome_id": "1234.5", "genome_name": "Test"}
    _, allowed = _render_bvbrc([genome])
    assert "[BV-BRC genome 1234.5]" in allowed


def test_probe_172_render_bvbrc_no_id_strict_raises():
    """Genome with no genome_id or id raises in strict mode."""
    with pytest.raises(ValueError):
        _render_bvbrc([{"genome_name": "No ID"}], strict=True)


def test_probe_173_render_bvbrc_no_id_lenient_skips():
    """Genome with no genome_id in lenient mode returns empty tokens."""
    _, allowed = _render_bvbrc([{"genome_name": "No ID"}], strict=False)
    assert len(allowed) == 0


def test_probe_174_render_bvbrc_fallback_id_key_accepted():
    """Genome with only ``id`` key (no ``genome_id``) still produces token."""
    genome = {"id": "FALLBACK-1", "genome_name": "Test"}
    _, allowed = _render_bvbrc([genome])
    assert "[BV-BRC genome FALLBACK-1]" in allowed


def test_probe_175_render_bvbrc_cap_limits_output():
    """cap=1 truncates to first genome only."""
    genomes = [{"genome_id": "G1"}, {"genome_id": "G2"}]
    _, allowed = _render_bvbrc(genomes, cap=1)
    assert "[BV-BRC genome G1]" in allowed
    assert "[BV-BRC genome G2]" not in allowed


# ---------------------------------------------------------------------------
# _render_violin_mappings output contracts (176-178)
# ---------------------------------------------------------------------------


def test_probe_176_render_violin_mapping_token_format():
    """Mapping produces [VIOLIN <synonym_id>] token."""
    mapping = {
        "synonym_id": "VOC-TEST",
        "query_term": "foo",
        "canonical_term": "Foo Vaccine",
    }
    _, allowed = _render_violin([mapping])
    assert "[VIOLIN VOC-TEST]" in allowed


def test_probe_177_render_violin_no_canonical_term_strict_raises():
    """Mapping missing canonical_term raises in strict mode."""
    with pytest.raises(ValueError):
        _render_violin([{"synonym_id": "VOC-1", "query_term": "foo"}], strict=True)


def test_probe_178_render_violin_fallback_id_key_accepted():
    """Mapping with ``id`` instead of ``synonym_id`` produces token."""
    mapping = {"id": "VOC-FALLBACK", "canonical_term": "Bar Vaccine"}
    _, allowed = _render_violin([mapping])
    assert "[VIOLIN VOC-FALLBACK]" in allowed


# ---------------------------------------------------------------------------
# _render_publications output contracts (179-180)
# ---------------------------------------------------------------------------


def test_probe_179_render_pub_doi_token_format():
    """Publication DOI produces [<doi>] token."""
    pub = {"doi": "10.1234/test.2020", "title": "Test paper"}
    _, allowed = _render_pubs([pub])
    assert "[10.1234/test.2020]" in allowed


def test_probe_180_render_pub_no_doi_strict_raises():
    """Publication without DOI raises in strict mode."""
    with pytest.raises(ValueError):
        _render_pubs([{"title": "No DOI paper"}], strict=True)
