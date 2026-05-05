"""Adversarial probes batch 5 — probes 121-150.

Targets: synthesizer edge cases — require_inline_citations=False,
validate_citations_against_inputs=False, min_response_chars enforcement,
fail_on_empty_retrieval=False bypass, lenient-mode skip vs strict-mode
raises, multiple inputs per source, hallucinated citations rejected,
and SynthesisConfig min/max field boundary conditions.

All synthesizer inputs are plain dicts (not typed dataclass objects).
Citation token formats:
  - RAG chunks:    [RAG chunk #1], [RAG chunk #2], ...
  - BV-BRC:        [BV-BRC genome <genome_id>]
  - VIOLIN:        [VIOLIN <synonym_id>]
  - Publications:  [<doi>]  (DOI wrapped in brackets)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_config(**overrides: Any):
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    base: dict[str, Any] = {
        "system_prompt": "You are a test assistant.",
        "min_response_chars": 0,
        "require_inline_citations": True,
        "min_distinct_citations": 1,
        "validate_citations_against_inputs": True,
        "fail_on_empty_retrieval": True,
        "strict_input_validation": True,
    }
    base.update(overrides)
    return SynthesisConfig(**base)


def _stub_llm(response: str):
    """Return a stub LLM that returns a fixed response via invoke()."""

    class _StubLLM:
        def invoke(self, messages):
            class _R:
                content = response

            return _R()

    return _StubLLM()


def _rag(text: str, *, chunk_id: str = "chunk-A", score: float = 0.9) -> dict[str, Any]:
    return {"id": chunk_id, "text": text, "score": score}


def _bvbrc(*, genome_id: str = "GEN-001", name: str = "Test genome") -> dict[str, Any]:
    return {"genome_id": genome_id, "genome_name": name}


def _violin(*, synonym_id: str = "VOC-001", canonical: str = "Test Vaccine") -> dict[str, Any]:
    return {
        "synonym_id": synonym_id,
        "query_term": "test",
        "canonical_term": canonical,
        "confidence": 0.9,
    }


def _pub(*, doi: str = "10.1234/test.doi", title: str = "Test paper") -> dict[str, Any]:
    return {"doi": doi, "title": title, "year": 2020}


# ---------------------------------------------------------------------------
# require_inline_citations=False path (121-125)
# ---------------------------------------------------------------------------


def test_probe_121_require_citations_false_no_citation_still_succeeds():
    """When require_inline_citations=False, a response with no citations passes."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Zika virus is transmitted by mosquitoes. No citation needed.")
    result = synthesize_response(
        "Tell me about Zika", llm=llm, config=cfg, rag_chunks=[_rag("Relevant info about Zika.")]
    )
    assert isinstance(result, str) and len(result) > 0


def test_probe_122_require_citations_false_with_citation_also_succeeds():
    """When require_inline_citations=False, citations present are accepted too."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Zika is a flavivirus [RAG chunk #1].")
    result = synthesize_response(
        "Zika", llm=llm, config=cfg, rag_chunks=[_rag("Zika is a flavivirus.")]
    )
    assert result is not None


def test_probe_123_require_citations_true_no_citation_raises():
    """When require_inline_citations=True, a response with zero citations raises."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Zika is a disease. No inline citation.")
    with pytest.raises(ValueError, match="[Cc]itation|inline"):
        synthesize_response(
            "Zika", llm=llm, config=cfg, rag_chunks=[_rag("Zika info.", chunk_id="chunk-1")]
        )


def test_probe_124_min_distinct_citations_0_with_citation_false_succeeds():
    """min_distinct_citations=0 disables the citation count floor."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Zika is a flavivirus [RAG chunk #1].")
    result = synthesize_response(
        "Zika", llm=llm, config=cfg, rag_chunks=[_rag("Zika data.", chunk_id="chunk-1")]
    )
    assert result is not None


def test_probe_125_min_distinct_citations_2_with_one_citation_raises():
    """min_distinct_citations=2 raises when response has only 1 distinct citation."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=2,
        validate_citations_against_inputs=True,
    )
    chunks = [
        _rag("Info 1.", chunk_id="chunk-1"),
        _rag("Info 2.", chunk_id="chunk-2"),
    ]
    # Response only has [RAG chunk #1] — one distinct citation < 2
    llm = _stub_llm("Answer [RAG chunk #1]. More details [RAG chunk #1].")
    with pytest.raises(ValueError, match="[Cc]itation|distinct"):
        synthesize_response("Query", llm=llm, config=cfg, rag_chunks=chunks)


# ---------------------------------------------------------------------------
# validate_citations_against_inputs=False path (126-130)
# ---------------------------------------------------------------------------


def test_probe_126_validate_citations_false_fabricated_pattern_token_passes():
    """With validate_citations_against_inputs=False, a valid-format but fabricated
    citation token (not in allowed_tokens) is accepted.

    NOTE: The token MUST match one of the four citation_marker_patterns regex
    shapes (e.g. [BV-BRC genome FAKE-ID]) to be counted by _extract_distinct_citations.
    An arbitrary string like [HALLUCINATED-999] produces 0 pattern matches and
    still fails min_distinct_citations even with validation off.
    """
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=False,
    )
    # [BV-BRC genome FAKE-ID] matches the BV-BRC pattern but is NOT in
    # allowed_tokens (no BV-BRC genome was passed). With validate=False, this passes.
    llm = _stub_llm("Answer based on data [BV-BRC genome FAKE-ID].")
    result = synthesize_response(
        "Query", llm=llm, config=cfg, rag_chunks=[_rag("Info.", chunk_id="chunk-1")]
    )
    assert result is not None


def test_probe_127_validate_citations_true_hallucinated_citation_raises():
    """With validate_citations_against_inputs=True, a hallucinated ID is rejected."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Answer [HALLUCINATED-999].")
    with pytest.raises(ValueError, match="[Hh]allucinated|not in allowed|citation"):
        synthesize_response(
            "Query", llm=llm, config=cfg, rag_chunks=[_rag("Info.", chunk_id="chunk-1")]
        )


def test_probe_128_validate_citations_true_valid_bvbrc_id_passes():
    """A citation matching a BV-BRC genome_id is accepted."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    genome = _bvbrc(genome_id="BV-001")
    # BV-BRC citation token format: [BV-BRC genome BV-001]
    llm = _stub_llm("Genome data from [BV-BRC genome BV-001].")
    result = synthesize_response("Query", llm=llm, config=cfg, bvbrc_genomes=[genome])
    assert result is not None


def test_probe_129_validate_citations_true_valid_violin_id_passes():
    """A citation matching a VIOLIN synonym_id is accepted."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    mapping = _violin(synonym_id="VOC-0000001")
    # VIOLIN citation token format: [VIOLIN VOC-0000001]
    llm = _stub_llm("Vaccine data from [VIOLIN VOC-0000001].")
    result = synthesize_response("Query", llm=llm, config=cfg, violin_mappings=[mapping])
    assert result is not None


def test_probe_130_allowed_tokens_union_covers_all_sources():
    """Allowed tokens include IDs from all four sources simultaneously."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    # Citation from each source type in the response
    llm = _stub_llm(
        "See [RAG chunk #1] and [BV-BRC genome BV-BETA] "
        "and [VIOLIN VOC-GAMMA] and [10.1234/valid.doi]."
    )
    result = synthesize_response(
        "Query",
        llm=llm,
        config=cfg,
        rag_chunks=[_rag("RAG info.", chunk_id="chunk-1")],
        bvbrc_genomes=[_bvbrc(genome_id="BV-BETA")],
        violin_mappings=[_violin(synonym_id="VOC-GAMMA")],
        publications=[_pub(doi="10.1234/valid.doi")],
    )
    assert result is not None


# ---------------------------------------------------------------------------
# min_response_chars enforcement (131-135)
# ---------------------------------------------------------------------------


def test_probe_131_min_response_chars_zero_any_length_passes():
    """min_response_chars=0 disables the length floor."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        min_response_chars=0,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("[RAG chunk #1]")
    result = synthesize_response(
        "q", llm=llm, config=cfg, rag_chunks=[_rag("x.", chunk_id="chunk-1")]
    )
    assert result is not None


def test_probe_132_min_response_chars_exceeded_raises():
    """A response shorter than min_response_chars raises ValueError."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        min_response_chars=200,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("[RAG chunk #1]")  # far shorter than 200 chars
    with pytest.raises(ValueError, match="min_response_chars|short|length|curtailed"):
        synthesize_response(
            "query", llm=llm, config=cfg, rag_chunks=[_rag("Short info.", chunk_id="chunk-1")]
        )


def test_probe_133_min_response_chars_met_exactly_passes():
    """A response at least min_response_chars long passes."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    target = 20
    cfg = _stub_config(
        min_response_chars=target,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    citation = "[RAG chunk #1]"
    # Build a response of at least target chars, with a citation
    padding = "A" * max(0, target - len(citation))
    llm = _stub_llm(f"{padding}{citation}")
    result = synthesize_response(
        "q", llm=llm, config=cfg, rag_chunks=[_rag("Info about virus.", chunk_id="chunk-1")]
    )
    assert result is not None


def test_probe_134_min_response_chars_negative_not_allowed():
    """min_response_chars must be >= 0; negative value should raise at config time."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    with pytest.raises((ValueError, Exception)):
        SynthesisConfig(
            system_prompt="test",
            min_response_chars=-1,
        )


def test_probe_135_min_response_chars_default_is_nonnegative():
    """Default min_response_chars is non-negative."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="test")
    assert cfg.min_response_chars >= 0


# ---------------------------------------------------------------------------
# fail_on_empty_retrieval (136-140)
# ---------------------------------------------------------------------------


def test_probe_136_fail_on_empty_retrieval_true_no_inputs_raises():
    """fail_on_empty_retrieval=True with all-empty inputs raises ValueError."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Empty answer.")
    with pytest.raises(ValueError, match="[Ee]mpty|retrieval|no.*input"):
        synthesize_response("q", llm=llm, config=cfg)


def test_probe_137_fail_on_empty_retrieval_false_no_inputs_returns_result():
    """fail_on_empty_retrieval=False with all-empty inputs calls LLM and returns."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("No retrieved data; general answer.")
    result = synthesize_response("q", llm=llm, config=cfg)
    assert result is not None


def test_probe_138_fail_on_empty_retrieval_true_with_one_chunk_passes():
    """fail_on_empty_retrieval=True passes when at least one chunk is provided."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Data [RAG chunk #1].")
    result = synthesize_response(
        "q", llm=llm, config=cfg, rag_chunks=[_rag("Pathogen info.", chunk_id="chunk-1")]
    )
    assert result is not None


def test_probe_139_fail_on_empty_retrieval_true_empty_bvbrc_list_same_as_no_input():
    """Passing an explicit empty list counts the same as not passing anything."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("General answer.")
    with pytest.raises(ValueError, match="[Ee]mpty|retrieval|no.*input"):
        synthesize_response("q", llm=llm, config=cfg, bvbrc_genomes=[])


def test_probe_140_fail_on_empty_retrieval_true_violin_only_passes():
    """VIOLIN mappings alone satisfy the non-empty-retrieval check."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    mapping = _violin(synonym_id="VOC-DELTA")
    llm = _stub_llm("Vaccine data [VIOLIN VOC-DELTA].")
    result = synthesize_response("q", llm=llm, config=cfg, violin_mappings=[mapping])
    assert result is not None


# ---------------------------------------------------------------------------
# strict vs lenient input validation (141-145)
# ---------------------------------------------------------------------------


def test_probe_141_strict_input_validation_bad_rag_chunk_raises():
    """strict_input_validation=True: a RAG chunk with empty text raises before LLM."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(strict_input_validation=True)
    bad_chunk = {"id": "chunk-1", "text": "", "score": 0.9}
    llm = _stub_llm("Response [RAG chunk #1].")
    with pytest.raises(ValueError):
        synthesize_response("q", llm=llm, config=cfg, rag_chunks=[bad_chunk])


def test_probe_142_lenient_input_validation_bad_rag_chunk_skips():
    """strict_input_validation=False: a RAG chunk with empty text is skipped not raised."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        strict_input_validation=False,
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    bad_chunk = {"id": "chunk-1", "text": "", "score": 0.9}
    llm = _stub_llm("Answer with no citations.")
    result = synthesize_response("q", llm=llm, config=cfg, rag_chunks=[bad_chunk])
    assert result is not None


def test_probe_143_strict_input_validation_doi_bracket_raises():
    """strict_input_validation=True: a DOI containing '[' is rejected at render time."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        strict_input_validation=True,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    bad_pub = {"doi": "10.1234/bad[doi]", "title": "Bad DOI paper", "year": 2020}
    llm = _stub_llm("Should never be reached.")
    with pytest.raises(ValueError, match="doi|bracket|\\[|character"):
        synthesize_response("q", llm=llm, config=cfg, publications=[bad_pub])


def test_probe_144_lenient_input_validation_doi_bracket_skips():
    """strict_input_validation=False: a DOI containing '[' is skipped, not raised."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        strict_input_validation=False,
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    bad_pub = {"doi": "10.1234/bad[doi]", "title": "Bad DOI paper", "year": 2020}
    llm = _stub_llm("General answer, no citations.")
    result = synthesize_response("q", llm=llm, config=cfg, publications=[bad_pub])
    assert result is not None


def test_probe_145_mixed_valid_and_invalid_pubs_strict_raises_on_bad():
    """In strict mode, a single bad DOI in a list of pubs raises."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(strict_input_validation=True)
    good_pub = {"doi": "10.1234/good.doi", "title": "Good paper", "year": 2020}
    bad_pub = {"doi": "10.5678/bad[doi]", "title": "Bad paper", "year": 2021}
    llm = _stub_llm("Would not be reached.")
    with pytest.raises(ValueError):
        synthesize_response("q", llm=llm, config=cfg, publications=[good_pub, bad_pub])


# ---------------------------------------------------------------------------
# Multiple inputs per source (146-150)
# ---------------------------------------------------------------------------


def test_probe_146_multiple_rag_chunks_all_rendered():
    """With 3 RAG chunks, all three appear as allowed citation tokens."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=3,
        validate_citations_against_inputs=True,
        max_rag_chunks=10,
    )
    chunks = [_rag(f"Info {i}.", chunk_id=f"chunk-{i}") for i in range(1, 4)]
    llm = _stub_llm("Data [RAG chunk #1] and [RAG chunk #2] and [RAG chunk #3].")
    result = synthesize_response("q", llm=llm, config=cfg, rag_chunks=chunks)
    assert result is not None


def test_probe_147_multiple_bvbrc_genomes_both_cited():
    """With 2 BV-BRC genomes, both IDs are valid citation tokens."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=2,
        validate_citations_against_inputs=True,
        max_bvbrc_genomes=5,
    )
    g1 = _bvbrc(genome_id="BV-G1")
    g2 = _bvbrc(genome_id="BV-G2")
    llm = _stub_llm("Genomes [BV-BRC genome BV-G1] and [BV-BRC genome BV-G2].")
    result = synthesize_response("q", llm=llm, config=cfg, bvbrc_genomes=[g1, g2])
    assert result is not None


def test_probe_148_max_cap_applied_excess_sources_truncated():
    """max_rag_chunks=2 means only 2 chunks produce tokens; chunk 3 token is invalid."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        max_rag_chunks=2,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    chunks = [_rag(f"Info {i}.", chunk_id=f"chunk-{i}") for i in range(1, 4)]
    # [RAG chunk #3] is beyond the cap — should trigger hallucination rejection
    llm = _stub_llm("Data [RAG chunk #3].")
    with pytest.raises(ValueError, match="[Hh]allucinated|not in allowed|citation"):
        synthesize_response("q", llm=llm, config=cfg, rag_chunks=chunks)


def test_probe_149_synthesize_response_returns_string():
    """Return type is a non-empty string on success."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Answer [RAG chunk #1].")
    result = synthesize_response(
        "q", llm=llm, config=cfg, rag_chunks=[_rag("Info.", chunk_id="chunk-1")]
    )
    assert isinstance(result, str)
    assert len(result) > 0


def test_probe_150_synthesize_response_contains_llm_output():
    """The returned string contains (or is derived from) the LLM's response."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Specific marker phrase [RAG chunk #1].")
    result = synthesize_response(
        "q", llm=llm, config=cfg, rag_chunks=[_rag("Relevant data.", chunk_id="chunk-1")]
    )
    assert "Specific marker phrase" in result
