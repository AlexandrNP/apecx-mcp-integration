"""Adversarial probes batch 2 — probes 031-060.

Targets: synthesis config bounds, citation extraction edge cases,
dictionary builder entry validation, OLS resolver field contracts,
normalization corner cases, DictionaryIndex API invariants.

Each probe targets a DISTINCT code path or input shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# SynthesisConfig / synthesize_response bounds (031-040)
# ---------------------------------------------------------------------------


def _minimal_synthesis_cfg(**overrides):
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    base = {
        "system_prompt": "Be concise.",
        "min_response_chars": 0,
        "require_inline_citations": True,
        "min_distinct_citations": 1,
        "validate_citations_against_inputs": True,
        "fail_on_empty_retrieval": True,
        "strict_input_validation": True,
    }
    base.update(overrides)
    return SynthesisConfig(**base)


class _Echo:
    """Stub LLM that echoes its first input message's content prefixed with the response."""

    def __init__(self, response: str) -> None:
        self._r = response

    def invoke(self, msgs):
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._r)


def test_probe_031_synthesizer_max_rag_chunks_cap_applied():
    """When max_rag_chunks=1, only the first chunk is rendered to the LLM.
    If the stub cites chunk #2, the grounding validator must reject it."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    chunks = [
        {"id": "a", "text": "first chunk"},
        {"id": "b", "text": "second chunk"},
    ]
    cfg = _minimal_synthesis_cfg(max_rag_chunks=1)
    stub = _Echo("[RAG chunk #1] first data")

    result = synthesize_response("Q", rag_chunks=chunks, llm=stub, config=cfg)
    assert "[RAG chunk #1]" in result
    # chunk #2 should not be in allowed_tokens, so we can't cite it
    with pytest.raises(ValueError, match="hallucinating"):
        bad_stub = _Echo("[RAG chunk #2] second data")
        synthesize_response("Q", rag_chunks=chunks, llm=bad_stub, config=cfg)


def test_probe_032_synthesizer_max_bvbrc_genomes_cap_applied():
    """When max_bvbrc_genomes=1, genome at index 1 is NOT rendered."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    genomes = [
        {"genome_id": "G1", "genome_name": "First"},
        {"genome_id": "G2", "genome_name": "Second"},
    ]
    cfg = _minimal_synthesis_cfg(max_bvbrc_genomes=1)
    stub = _Echo("[BV-BRC genome G1] data")
    result = synthesize_response("Q", bvbrc_genomes=genomes, llm=stub, config=cfg)
    assert "[BV-BRC genome G1]" in result

    with pytest.raises(ValueError, match="hallucinating"):
        bad_stub = _Echo("[BV-BRC genome G2] data")
        synthesize_response("Q", bvbrc_genomes=genomes, llm=bad_stub, config=cfg)


def test_probe_033_synthesizer_max_publications_cap_applied():
    """When max_publications=1, only the first DOI is authorized."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    pubs = [
        {"doi": "10.1/first", "title": "First"},
        {"doi": "10.1/second", "title": "Second"},
    ]
    cfg = _minimal_synthesis_cfg(max_publications=1)
    stub = _Echo("[10.1/first] cited")
    result = synthesize_response("Q", publications=pubs, llm=stub, config=cfg)
    assert "[10.1/first]" in result

    with pytest.raises(ValueError, match="hallucinating"):
        bad_stub = _Echo("[10.1/second] cited")
        synthesize_response("Q", publications=pubs, llm=bad_stub, config=cfg)


def test_probe_034_synthesizer_max_violin_cap_applied():
    """When max_violin_mappings=1, mapping at index 1 is NOT authorized."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    mappings = [
        {"synonym_id": "V1", "canonical_term": "Term1"},
        {"synonym_id": "V2", "canonical_term": "Term2"},
    ]
    cfg = _minimal_synthesis_cfg(max_violin_mappings=1)
    stub = _Echo("[VIOLIN V1] data")
    result = synthesize_response("Q", violin_mappings=mappings, llm=stub, config=cfg)
    assert "[VIOLIN V1]" in result

    with pytest.raises(ValueError, match="hallucinating"):
        bad_stub = _Echo("[VIOLIN V2] data")
        synthesize_response("Q", violin_mappings=mappings, llm=bad_stub, config=cfg)


def test_probe_035_synthesizer_empty_query_raises_before_llm():
    """An empty query must raise immediately, not reach the LLM."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _minimal_synthesis_cfg(fail_on_empty_retrieval=False)
    stub = _Echo("[RAG chunk #1] ok")

    with pytest.raises(ValueError, match="non-empty string"):
        synthesize_response("", rag_chunks=[{"text": "x"}], llm=stub, config=cfg)


def test_probe_036_synthesizer_whitespace_only_query_raises():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _minimal_synthesis_cfg(fail_on_empty_retrieval=False)
    stub = _Echo("[RAG chunk #1] ok")
    with pytest.raises(ValueError, match="non-empty string"):
        synthesize_response("   ", rag_chunks=[{"text": "x"}], llm=stub, config=cfg)


def test_probe_037_synthesizer_llm_returns_empty_string_raises():
    """LLM returning empty string must raise, not return silently."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    class _EmptyLLM:
        def invoke(self, msgs):
            from langchain_core.messages import AIMessage

            return AIMessage(content="")

    cfg = _minimal_synthesis_cfg(require_inline_citations=False)
    with pytest.raises(ValueError, match="empty"):
        synthesize_response(
            "Q",
            rag_chunks=[{"text": "some text"}],
            llm=_EmptyLLM(),
            config=cfg,
        )


def test_probe_038_synthesizer_doi_with_bracket_rejected_in_strict():
    """A DOI containing '[' cannot match the citation extraction pattern.
    The renderer must reject it in strict mode with a clear error so the
    failure is surfaced at render time, not as a confusing '0 citations'
    error downstream."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    bad_pub = {"doi": "10.1/abc[x]def", "title": "Bad"}
    cfg = _minimal_synthesis_cfg()

    with pytest.raises(ValueError, match="contract violation"):
        synthesize_response(
            "Q",
            publications=[bad_pub],
            llm=_Echo("[10.1/abc[x]def] cited"),
            config=cfg,
        )


def test_probe_039_synthesizer_rag_chunk_score_none_does_not_crash():
    """A RAG chunk with score=None should render without a similarity line,
    not raise a formatting error."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    chunk = {"id": "c1", "source": "paper.pdf", "score": None, "text": "Some text."}
    cfg = _minimal_synthesis_cfg()
    stub = _Echo("[RAG chunk #1] cited")
    result = synthesize_response("Q", rag_chunks=[chunk], llm=stub, config=cfg)
    assert "[RAG chunk #1]" in result


def test_probe_040_synthesizer_violin_mapping_confidence_none_does_not_crash():
    """A VIOLIN mapping with confidence=None should render without the
    confidence line, not raise."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    mapping = {"synonym_id": "V99", "canonical_term": "Term", "confidence": None}
    cfg = _minimal_synthesis_cfg()
    stub = _Echo("[VIOLIN V99] ok")
    result = synthesize_response("Q", violin_mappings=[mapping], llm=stub, config=cfg)
    assert "[VIOLIN V99]" in result


# ---------------------------------------------------------------------------
# Citation extraction invariants (041-050)
# ---------------------------------------------------------------------------


def test_probe_041_extract_citations_empty_text_returns_empty_set():
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations("", cfg.citation_marker_patterns)
    assert result == set()


def test_probe_042_extract_citations_deduplicates_repeated_token():
    """Citing the same genome 10 times = 1 distinct citation, not 10."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = " ".join(["[BV-BRC genome G1]"] * 10)
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert result == {"[BV-BRC genome G1]"}
    assert len(result) == 1


def test_probe_043_extract_citations_multiple_patterns_merged():
    """Records from all four pattern types must be merged into one set."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[RAG chunk #1] [BV-BRC genome G1] [VIOLIN V1] [10.1/x]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert len(result) == 4


def test_probe_044_extract_citations_partial_token_not_matched():
    """A truncated citation like '[RAG chunk #' is not matched — patterns
    require the full token including closing bracket."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[RAG chunk #"  # missing closing bracket and number
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert result == set()


def test_probe_045_extract_citations_adjacent_tokens_not_merged():
    """Two adjacent citation tokens [BV-BRC genome G1][BV-BRC genome G2]
    must be extracted as two separate tokens, not one merged token."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[BV-BRC genome G1][BV-BRC genome G2]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert "[BV-BRC genome G1]" in result
    assert "[BV-BRC genome G2]" in result


def test_probe_046_extract_citations_doi_with_slash_variants():
    """DOIs contain multiple slashes and hyphens — pattern must match them."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[10.1056/NEJMoa1414905] [10.1038/s41586-021-03454-z]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert "[10.1056/NEJMoa1414905]" in result
    assert "[10.1038/s41586-021-03454-z]" in result


def test_probe_047_extract_citations_rag_chunk_numbering_sequential():
    """[RAG chunk #0] is NOT a valid citation (1-indexed); the pattern
    requires at least one digit after #."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[RAG chunk #0] [RAG chunk #1] [RAG chunk #99]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    # Patterns match \d+ which includes 0 — but chunk #0 would never be
    # authorized (1-indexed). This probe checks that the pattern itself
    # matches the syntax, not that 0 is semantically valid.
    assert "[RAG chunk #1]" in result
    assert "[RAG chunk #99]" in result


def test_probe_048_extract_citations_violin_id_with_dots_and_dashes():
    """VIOLIN IDs can contain dots and dashes — pattern must match them."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[VIOLIN VOC-0012.3]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert "[VIOLIN VOC-0012.3]" in result


def test_probe_049_extract_citations_whitespace_inside_bracket_not_matched():
    """A token with internal whitespace like [BV-BRC genome G 1] must NOT
    match — the pattern excludes whitespace inside the ID."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "[BV-BRC genome G 1]"
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert result == set()


def test_probe_050_extract_citations_no_false_positives_in_prose():
    """Plain prose with brackets like [see refs above] must not match."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    text = "See [see refs above] and [1, 2, 3] for more information."
    cfg = _minimal_synthesis_cfg()
    result = _extract_distinct_citations(text, cfg.citation_marker_patterns)
    assert result == set()


# ---------------------------------------------------------------------------
# DictionaryIndex API invariants (051-060)
# ---------------------------------------------------------------------------


def _build_minimal_index():
    """Build a minimal in-memory DictionaryIndex for testing."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry

    now = datetime.now(tz=UTC)
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_11021",
        canonical_label="Eastern equine encephalitis virus",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.99,
        resolved_at=now,
        source_records=("EEEV_record",),
        synonyms=("EEEV", "Eastern equine encephalitis"),
    )
    manifest = BuildManifest(
        dictionary_version="test-0.1",
        built_at=now,
        ontology_versions={"ncbitaxon": "2024-01-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    inverse = {}
    for surface in (entry.canonical_label, *entry.synonyms):
        norm = normalize_surface_form(surface)
        if norm:
            inverse[(entry.entity_type.value, norm)] = entry.canonical_iri

    return DictionaryIndex(
        inverse=inverse,
        entries={entry.canonical_iri: entry},
        manifest=manifest,
    )


def test_probe_051_dictionary_index_lookup_exact_match():
    """Exact synonym lookup returns the expected entry."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    idx = _build_minimal_index()
    entry = idx.lookup(EntityType.PATHOGEN, "EEEV")
    assert entry is not None
    assert entry.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_11021"


def test_probe_052_dictionary_index_lookup_case_insensitive():
    """Lookup is case-insensitive — 'eeev' and 'EEEV' must match the same entry."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    idx = _build_minimal_index()
    lower = idx.lookup(EntityType.PATHOGEN, "eeev")
    upper = idx.lookup(EntityType.PATHOGEN, "EEEV")
    assert lower is not None
    assert upper is not None
    assert lower.canonical_iri == upper.canonical_iri


def test_probe_053_dictionary_index_lookup_wrong_entity_type_misses():
    """Lookup with wrong entity type must miss, not return a pathogen entry."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    idx = _build_minimal_index()
    result = idx.lookup(EntityType.VACCINE, "EEEV")
    assert result is None


def test_probe_054_dictionary_index_lookup_empty_normalized_misses():
    """A surface form that normalizes to empty string must return None."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    idx = _build_minimal_index()
    result = idx.lookup(EntityType.PATHOGEN, "   ")
    assert result is None


def test_probe_055_dictionary_index_lookup_any_type_finds_pathogen():
    """lookup_any_type returns entries across entity types when not specified."""
    idx = _build_minimal_index()
    results = idx.lookup_any_type("EEEV")
    assert len(results) >= 1
    iris = {e.canonical_iri for e in results}
    assert "http://purl.obolibrary.org/obo/NCBITaxon_11021" in iris


def test_probe_056_dictionary_index_lookup_any_type_empty_returns_empty():
    idx = _build_minimal_index()
    results = idx.lookup_any_type("   ")
    assert results == []


def test_probe_057_dictionary_index_lookup_by_iri_returns_entry():
    idx = _build_minimal_index()
    iri = "http://purl.obolibrary.org/obo/NCBITaxon_11021"
    entry = idx.lookup_by_iri(iri)
    assert entry is not None
    assert entry.canonical_iri == iri


def test_probe_058_dictionary_index_lookup_by_iri_unknown_returns_none():
    idx = _build_minimal_index()
    result = idx.lookup_by_iri("http://purl.obolibrary.org/obo/NCBITaxon_9999999")
    assert result is None


def test_probe_059_dictionary_index_entry_count_correct():
    idx = _build_minimal_index()
    assert idx.entry_count() == 1


def test_probe_060_dictionary_index_no_hierarchy_lookup_ancestor_returns_none():
    """Without a taxon hierarchy, lookup_ancestor must always return None."""
    idx = _build_minimal_index()
    result = idx.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_11021")
    assert result is None
