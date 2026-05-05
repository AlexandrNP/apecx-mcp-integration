from __future__ import annotations

import os
from typing import Any

import pytest
from apecx_integration.agents.rag_synthesis.synthesizer import (
    SynthesisConfig,
    _extract_distinct_citations,
    synthesize_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubLLM:
    """Deterministic LLM stub that echoes all supplied citation tokens back.

    The stub response is deliberately long enough to clear
    ``min_response_chars`` (set to 0 in ``_stub_config``) and includes one
    citation token from each of the four retrieval sources so the validator
    can confirm all sources reached the prompt.
    """

    def __init__(self, response_text: str) -> None:
        self._text = response_text

    def invoke(self, messages: list[Any]) -> Any:
        class _R:
            def __init__(self, text: str) -> None:
                self.content = text

        return _R(self._text)


def _stub_config(**overrides: Any) -> SynthesisConfig:
    """Return a SynthesisConfig with test-friendly defaults.

    Disables ``min_response_chars`` (stub responses are short) and
    ``fail_on_empty_retrieval`` is left on so callers must still supply
    real data. ``validate_citations_against_inputs`` stays on.
    """
    base: dict[str, Any] = {
        "system_prompt": "You are a test assistant. Cite retrieved data.",
        "min_response_chars": 0,
        "require_inline_citations": True,
        "min_distinct_citations": 1,
        "validate_citations_against_inputs": True,
        "fail_on_empty_retrieval": True,
        "strict_input_validation": True,
    }
    base.update(overrides)
    return SynthesisConfig(**base)


# ---------------------------------------------------------------------------
# Fixture data — realistic shapes matching real pipeline outputs
# ---------------------------------------------------------------------------

_RAG_CHUNKS: list[dict[str, Any]] = [
    {
        "id": "chunk-001",
        "source": "ebola_review_2014.pdf",
        "score": 0.94,
        "text": (
            "Ebola virus disease (EVD) is caused by Ebola virus (EBOV), a "
            "member of the Filoviridae family. The 2014 West Africa outbreak "
            "was the largest in recorded history, with over 28,000 cases."
        ),
    },
    {
        "id": "chunk-002",
        "source": "vaccine_platforms_review.pdf",
        "score": 0.89,
        "text": (
            "Recombinant vesicular stomatitis virus (rVSV) vectors have been "
            "used successfully as vaccine platforms for Ebola and other "
            "hemorrhagic fever viruses. rVSV-ZEBOV (Ervebo) was the first "
            "approved Ebola vaccine."
        ),
    },
]

_BVBRC_GENOMES: list[dict[str, Any]] = [
    {
        "genome_id": "1229193.6",
        "genome_name": "Ebola virus/H.sapiens-wt/GIN/2014/Makona-Gueckedou-C05",
        "taxon_lineage": "Filoviridae > Ebolavirus > Zaire ebolavirus",
        "host_name": "Homo sapiens",
    },
    {
        "genome_id": "1408250.3",
        "genome_name": "Sudan ebolavirus isolate Sudan/1976/Boniface",
        "taxon_lineage": "Filoviridae > Ebolavirus > Sudan ebolavirus",
        "host_name": "Homo sapiens",
    },
]

_VIOLIN_MAPPINGS: list[dict[str, Any]] = [
    {
        "synonym_id": "VOC-0012",
        "query_term": "Ervebo",
        "canonical_term": "rVSV-ZEBOV Ebola vaccine",
        "confidence": 0.97,
    },
    {
        "synonym_id": "VOC-0013",
        "query_term": "VSV-EBOLA",
        "canonical_term": "rVSV-ZEBOV Ebola vaccine",
        "confidence": 0.91,
    },
]

_PUBLICATIONS: list[dict[str, Any]] = [
    {
        "doi": "10.1056/NEJMoa1414905",
        "title": "Replication-Competent VSV-ZEBOV Ebola Vaccine in Humans",
        "authors": ["John Doe", "Jane Smith"],
        "year": 2015,
        "journal": "New England Journal of Medicine",
        "abstract": (
            "We assessed the safety and immunogenicity of a recombinant "
            "vesicular stomatitis virus-based vaccine against Zaire Ebola "
            "virus in a phase 1 clinical trial."
        ),
    }
]


# ---------------------------------------------------------------------------
# Tests: stub LLM (unconditional, no network needed)
# ---------------------------------------------------------------------------


def test_stub_llm_all_four_sources_reach_llm_prompt() -> None:
    """All four retrieval sources must produce citation tokens in the prompt.

    The stub LLM returns a canned response that includes one citation from
    each source type. If any source fails to render (skipped, wrong key),
    the allowed_tokens set shrinks and the grounding validator rejects the
    corresponding citation — causing the test to fail with a clear message.
    """
    response_text = (
        "Based on retrieved data: "
        "[RAG chunk #1] describes EVD epidemiology. "
        "[BV-BRC genome 1229193.6] is the 2014 Makona strain. "
        "[VIOLIN VOC-0012] maps Ervebo to its canonical name. "
        "[10.1056/NEJMoa1414905] reports phase 1 safety data."
    )
    stub = _StubLLM(response_text)
    cfg = _stub_config(min_distinct_citations=4)

    result = synthesize_response(
        "What do we know about Ebola vaccines?",
        rag_chunks=_RAG_CHUNKS,
        bvbrc_genomes=_BVBRC_GENOMES,
        violin_mappings=_VIOLIN_MAPPINGS,
        publications=_PUBLICATIONS,
        llm=stub,
        config=cfg,
    )

    distinct = _extract_distinct_citations(result, cfg.citation_marker_patterns)
    assert "[RAG chunk #1]" in distinct, f"RAG citation missing from: {distinct}"
    assert "[BV-BRC genome 1229193.6]" in distinct, f"BV-BRC citation missing: {distinct}"
    assert "[VIOLIN VOC-0012]" in distinct, f"VIOLIN citation missing: {distinct}"
    assert "[10.1056/NEJMoa1414905]" in distinct, f"DOI citation missing: {distinct}"
    assert len(distinct) == 4


def test_stub_llm_hallucinated_citation_rejected() -> None:
    """Citation-grounding validator must reject IDs invented by the LLM."""
    response_text = (
        "[RAG chunk #1] shows data. " "[BV-BRC genome 99999.99] is a hallucinated genome."
    )
    stub = _StubLLM(response_text)
    cfg = _stub_config(
        validate_citations_against_inputs=True,
        min_distinct_citations=1,
    )

    with pytest.raises(ValueError, match="hallucinating IDs"):
        synthesize_response(
            "Tell me about Ebola",
            rag_chunks=_RAG_CHUNKS,
            bvbrc_genomes=_BVBRC_GENOMES,
            violin_mappings=_VIOLIN_MAPPINGS,
            publications=_PUBLICATIONS,
            llm=stub,
            config=cfg,
        )


def test_stub_llm_empty_retrieval_rejected() -> None:
    """Empty retrieval inputs must raise before invoking the LLM."""
    stub = _StubLLM("[RAG chunk #1] something")
    cfg = _stub_config(fail_on_empty_retrieval=True)

    with pytest.raises(ValueError, match="every retrieval input is empty"):
        synthesize_response(
            "Query with no data",
            llm=stub,
            config=cfg,
        )


def test_stub_llm_curtailed_response_rejected() -> None:
    """Response below min_response_chars must be rejected."""
    stub = _StubLLM("[RAG chunk #1]")
    cfg = _stub_config(min_response_chars=500)

    with pytest.raises(ValueError, match="curtailed"):
        synthesize_response(
            "What do we know about Ebola?",
            rag_chunks=_RAG_CHUNKS,
            llm=stub,
            config=cfg,
        )


def test_stub_llm_rag_only_sufficient() -> None:
    """RAG-only retrieval (no BV-BRC/VIOLIN/publications) still works."""
    response_text = (
        "EVD was the deadliest outbreak in 2014 [RAG chunk #1]. "
        "The rVSV-ZEBOV vaccine was later approved [RAG chunk #2]. "
        "Detailed analysis confirms filovirus pathogenesis."
    )
    stub = _StubLLM(response_text)
    cfg = _stub_config(min_distinct_citations=2)

    result = synthesize_response(
        "Tell me about Ebola outbreaks",
        rag_chunks=_RAG_CHUNKS,
        llm=stub,
        config=cfg,
    )
    assert "[RAG chunk #1]" in result
    assert "[RAG chunk #2]" in result


def test_stub_llm_rag_chunk_missing_text_strict_mode() -> None:
    """A RAG chunk with no text field must raise in strict mode."""
    bad_chunks = [{"id": "c1", "source": "x.pdf"}]
    stub = _StubLLM("[RAG chunk #1] something")
    cfg = _stub_config()

    with pytest.raises(ValueError, match="contract violation"):
        synthesize_response(
            "Query",
            rag_chunks=bad_chunks,
            llm=stub,
            config=cfg,
        )


def test_stub_llm_publication_no_doi_strict_mode() -> None:
    """A publication without a DOI must raise in strict mode."""
    bad_pubs = [{"title": "Some paper", "authors": ["A. Author"]}]
    stub = _StubLLM("[10.1056/NEJMoa1414905] data")
    cfg = _stub_config()

    with pytest.raises(ValueError, match="contract violation"):
        synthesize_response(
            "What about Ebola vaccines?",
            publications=bad_pubs,
            llm=stub,
            config=cfg,
        )


def test_stub_llm_violin_mapping_no_id_strict_mode() -> None:
    """A VIOLIN mapping without a synonym_id must raise in strict mode."""
    bad_mappings = [{"canonical_term": "rVSV-ZEBOV"}]
    stub = _StubLLM("[VIOLIN VOC-0012] data")
    cfg = _stub_config()

    with pytest.raises(ValueError, match="contract violation"):
        synthesize_response(
            "About Ervebo",
            violin_mappings=bad_mappings,
            llm=stub,
            config=cfg,
        )


def test_stub_llm_bvbrc_genome_no_id_strict_mode() -> None:
    """A BV-BRC genome without genome_id must raise in strict mode."""
    bad_genomes = [{"genome_name": "Ebola virus/..."}]
    stub = _StubLLM("[BV-BRC genome 1229193.6] data")
    cfg = _stub_config()

    with pytest.raises(ValueError, match="contract violation"):
        synthesize_response(
            "Ebola genome data",
            bvbrc_genomes=bad_genomes,
            llm=stub,
            config=cfg,
        )


# ---------------------------------------------------------------------------
# Tests: harvester adapter (DataCite → publication dict round-trip)
# ---------------------------------------------------------------------------


def test_datacite_to_publication_round_trip() -> None:
    """DataCite → flat dict conversion must preserve all key fields."""
    from apecx_harvesters.loaders.base.model import (
        Creator,
        DataCite,
        Description,
        DescriptionType,
        Identifier,
        Publisher,
        Title,
    )
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=Identifier(
            identifier="10.1056/NEJMoa1414905",
            identifierType="DOI",
        ),
        titles=[Title(title="rVSV-ZEBOV Ebola Vaccine Phase 1")],
        creators=[
            Creator(givenName="John", familyName="Doe", name="Doe, John"),
            Creator(givenName="Jane", familyName="Smith", name="Smith, Jane"),
        ],
        publicationYear="2015",
        publisher=Publisher(name="New England Journal of Medicine"),
        descriptions=[
            Description(
                description="Phase 1 safety trial results.",
                descriptionType=DescriptionType.Abstract,
            )
        ],
    )

    pub = datacite_to_publication(record)

    assert pub["doi"] == "10.1056/NEJMoa1414905"
    assert "rVSV-ZEBOV" in pub["title"]
    assert "John Doe" in pub["authors"]
    assert pub["year"] == "2015"
    assert pub["journal"] == "New England Journal of Medicine"
    assert "Phase 1" in pub["abstract"]


def test_datacite_to_publication_missing_doi_raises() -> None:
    """Records without a DOI identifier must raise."""
    from apecx_harvesters.loaders.base.model import (
        DataCite,
        Identifier,
        Publisher,
        Title,
    )
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=Identifier(
            identifier="ark:/13030/m5br8st1",
            identifierType="ARK",
        ),
        titles=[Title(title="Non-DOI record")],
        creators=[],
        publisher=Publisher(name="Some Publisher"),
        publicationYear="2020",
    )

    with pytest.raises(ValueError, match="identifierType"):
        datacite_to_publication(record)


def test_datacite_to_publication_in_synthesizer() -> None:
    """DataCite record converted via adapter must work end-to-end in synthesizer."""
    from apecx_harvesters.loaders.base.model import (
        Creator,
        DataCite,
        Identifier,
        Publisher,
        Title,
    )
    from apecx_integration.agents.rag_synthesis.harvester_adapter import (
        datacite_to_publication,
    )

    record = DataCite(
        identifier=Identifier(
            identifier="10.1016/j.vaccine.2020.01.001",
            identifierType="DOI",
        ),
        titles=[Title(title="Ebola vaccine efficacy study")],
        creators=[Creator(givenName="A.", familyName="Researcher", name="Researcher, A.")],
        publisher=Publisher(name="Vaccine Journal"),
        publicationYear="2020",
    )
    pub = datacite_to_publication(record)

    response_text = (
        "This study [10.1016/j.vaccine.2020.01.001] examined efficacy. "
        "BV-BRC data [BV-BRC genome 1229193.6] supports the findings."
    )
    stub = _StubLLM(response_text)
    cfg = _stub_config(min_distinct_citations=2)

    result = synthesize_response(
        "Ebola vaccine efficacy",
        bvbrc_genomes=_BVBRC_GENOMES,
        publications=[pub],
        llm=stub,
        config=cfg,
    )
    assert "[10.1016/j.vaccine.2020.01.001]" in result
    assert "[BV-BRC genome 1229193.6]" in result


# ---------------------------------------------------------------------------
# Test: live LLM (gated on APECX_LLM_BASE_URL env var)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("APECX_LLM_BASE_URL"),
    reason="APECX_LLM_BASE_URL not set — skipping live LLM synthesis test",
)
def test_live_llm_synthesis_non_trivial_response() -> None:
    """Live LLM must produce a non-trivial Markdown response citing all sources.

    Run with::

        APECX_LLM_BASE_URL=http://localhost:11434/v1 \\
        APECX_LLM_MODEL=mistral-nemo:latest \\
        APECX_LLM_TEMPERATURE=0.0 \\
        APECX_LLM_MAX_TOKENS=2048 \\
        APECX_LLM_API_KEY=unused \\
        PYTHONPATH=src .venv/bin/python -m pytest \\
          tests/integration/test_e2e_rag_harvester_llm.py::test_live_llm_synthesis_non_trivial_response -v

    Assertions:
    - Response is at least 200 chars (non-trivial, not curtailed).
    - At least 2 distinct inline citation tokens present.
    - All cited tokens are in the allowed set (no hallucination).
    """
    from apecx_integration.agents._llm_factory import build_chat_llm

    llm = build_chat_llm()
    cfg = _stub_config(
        system_prompt=(
            "You are a biomedical research assistant. Synthesize the "
            "provided data into a concise Markdown report. Cite every "
            "factual claim using EXACTLY the inline citation tokens shown "
            "in the data (e.g. [RAG chunk #1], [BV-BRC genome 1229193.6], "
            "[VIOLIN VOC-0012], [10.1056/NEJMoa1414905]). Never invent "
            "citation IDs not present in the context."
        ),
        min_response_chars=200,
        min_distinct_citations=2,
        validate_citations_against_inputs=True,
    )

    result = synthesize_response(
        (
            "Summarize what is known about the rVSV-ZEBOV Ebola vaccine, "
            "including the 2014 outbreak strains, the VIOLIN synonym mappings, "
            "and the clinical trial publication."
        ),
        rag_chunks=_RAG_CHUNKS,
        bvbrc_genomes=_BVBRC_GENOMES,
        violin_mappings=_VIOLIN_MAPPINGS,
        publications=_PUBLICATIONS,
        llm=llm,
        config=cfg,
    )

    assert (
        len(result.strip()) >= 200
    ), f"Live LLM response is curtailed (len={len(result.strip())}): {result!r}"
    distinct = _extract_distinct_citations(result, cfg.citation_marker_patterns)
    assert (
        len(distinct) >= 2
    ), f"Live LLM cited only {len(distinct)} distinct source(s): {sorted(distinct)}"
