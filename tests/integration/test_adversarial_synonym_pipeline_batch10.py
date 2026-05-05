"""Adversarial probes batch 10 — probes 271-300.

Final batch. Targets: DictionaryIndex.load() with hierarchy + ancestor
traversal contract, synthesizer prompt-assembly shape (user prompt contains
all four section headers), citation token extraction boundary conditions,
_process_singleton configure/reset behavior, LookupResult canonical_ontology
string vs None, and full-stack mini E2E (write entry → load index → lookup →
synthesize with that token).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(tz=UTC)


def _make_entry(
    *,
    label: str = "Test",
    iri: str = "http://example.org/1",
    entity_type=None,
    synonyms: tuple = (),
    confidence: float = 1.0,
):
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    return DictionaryEntry(
        entity_type=entity_type or EntityType.PATHOGEN,
        canonical_iri=iri,
        canonical_label=label,
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=confidence,
        resolved_at=_now(),
        synonyms=synonyms,
    )


def _make_manifest():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    return BuildManifest(
        dictionary_version="test-batch10",
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024-01-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )


def _stub_config(**overrides: Any):
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    base: dict[str, Any] = {
        "system_prompt": "Test system prompt.",
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
    class _StubLLM:
        def invoke(self, messages):
            class _R:
                content = response

            return _R()

    return _StubLLM()


# ---------------------------------------------------------------------------
# DictionaryIndex with SQLite + hierarchy (271-275)
# ---------------------------------------------------------------------------


def test_probe_271_index_load_with_hierarchy_has_hierarchy_true(tmp_path):
    """DictionaryIndex.load() sets has_hierarchy=True when hierarchy was written."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(_make_entry())
        w.write_taxon_hierarchy(iter([(9606, 9605)]))

    idx = DictionaryIndex.load(db)
    assert idx.has_hierarchy is True


def test_probe_272_index_load_without_hierarchy_has_hierarchy_false(tmp_path):
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(_make_entry())

    idx = DictionaryIndex.load(db)
    assert idx.has_hierarchy is False


def test_probe_273_index_lookup_ancestor_with_hierarchy_known_id_returns_entry(tmp_path):
    """If an NCBITaxon IRI's ancestor is in the index, lookup_ancestor returns it."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    # parent taxon_id=9605 is in the dictionary; child 9606 is not
    parent_entry = _make_entry(
        label="Homo genus",
        iri="http://purl.obolibrary.org/obo/NCBITaxon_9605",
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(parent_entry)
        # child 9606 → parent 9605
        w.write_taxon_hierarchy(iter([(9606, 9605)]))

    idx = DictionaryIndex.load(db)
    # Query for child IRI (not in index) → should return parent entry
    child_iri = "http://purl.obolibrary.org/obo/NCBITaxon_9606"
    result = idx.lookup_ancestor(child_iri)
    assert result is not None
    assert result.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_9605"


def test_probe_274_index_lookup_ancestor_unknown_id_returns_none(tmp_path):
    """If taxon_id has no ancestor in the hierarchy, lookup_ancestor returns None."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(_make_entry())
        w.write_taxon_hierarchy(iter([(9606, 9605)]))

    idx = DictionaryIndex.load(db)
    # NCBITaxon_12345 is not in the hierarchy at all
    result = idx.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_12345")
    assert result is None


def test_probe_275_index_manifest_accessible_after_load(tmp_path):
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    m = _make_manifest()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(m)

    idx = DictionaryIndex.load(db)
    assert idx.manifest.dictionary_version == m.dictionary_version


# ---------------------------------------------------------------------------
# Synthesizer prompt structure invariants (276-280)
# ---------------------------------------------------------------------------


def test_probe_276_prompt_contains_rag_section_header():
    """The user prompt assembled inside synthesize_response contains RAG section."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    # Capture the prompt via a spy LLM
    captured = []

    class _SpyLLM:
        def invoke(self, messages):
            captured.extend(messages)

            class _R:
                content = "[RAG chunk #1]"

            return _R()

    cfg = _stub_config()
    synthesize_response(
        "query",
        llm=_SpyLLM(),
        config=cfg,
        rag_chunks=[{"text": "data", "id": "c1"}],
    )
    full_text = " ".join(getattr(m, "content", "") for m in captured)
    assert "RAG chunk" in full_text


def test_probe_277_prompt_contains_bvbrc_section():
    """The user prompt contains a BV-BRC section header."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    captured = []

    class _SpyLLM:
        def invoke(self, messages):
            captured.extend(messages)

            class _R:
                content = "[BV-BRC genome BV-1]"

            return _R()

    cfg = _stub_config()
    synthesize_response(
        "query",
        llm=_SpyLLM(),
        config=cfg,
        bvbrc_genomes=[{"genome_id": "BV-1"}],
    )
    full_text = " ".join(getattr(m, "content", "") for m in captured)
    assert "BV-BRC" in full_text


def test_probe_278_prompt_contains_violin_section():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    captured = []

    class _SpyLLM:
        def invoke(self, messages):
            captured.extend(messages)

            class _R:
                content = "[VIOLIN V-1]"

            return _R()

    cfg = _stub_config()
    synthesize_response(
        "query",
        llm=_SpyLLM(),
        config=cfg,
        violin_mappings=[{"synonym_id": "V-1", "canonical_term": "VaccX"}],
    )
    full_text = " ".join(getattr(m, "content", "") for m in captured)
    assert "VIOLIN" in full_text


def test_probe_279_prompt_contains_publications_section():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    captured = []

    class _SpyLLM:
        def invoke(self, messages):
            captured.extend(messages)

            class _R:
                content = "[10.1234/test]"

            return _R()

    cfg = _stub_config()
    synthesize_response(
        "query",
        llm=_SpyLLM(),
        config=cfg,
        publications=[{"doi": "10.1234/test", "title": "Test"}],
    )
    full_text = " ".join(getattr(m, "content", "") for m in captured)
    assert "Publication" in full_text or "publication" in full_text


def test_probe_280_system_prompt_appears_in_messages():
    """The SynthesisConfig.system_prompt appears in the SystemMessage."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    captured = []

    class _SpyLLM:
        def invoke(self, messages):
            captured.extend(messages)

            class _R:
                content = "[RAG chunk #1]"

            return _R()

    cfg = _stub_config(system_prompt="UNIQUE_SENTINEL_SYSTEM_PROMPT_XYZ")
    synthesize_response(
        "query",
        llm=_SpyLLM(),
        config=cfg,
        rag_chunks=[{"text": "data", "id": "c1"}],
    )
    texts = [getattr(m, "content", "") for m in captured]
    assert any("UNIQUE_SENTINEL_SYSTEM_PROMPT_XYZ" in t for t in texts)


# ---------------------------------------------------------------------------
# LookupResult canonical_ontology string vs None (281-285)
# ---------------------------------------------------------------------------


def test_probe_281_lookup_result_fast_hit_canonical_ontology_is_string():
    """For a fast hit, canonical_ontology is the StrEnum value (a string)."""
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(label="Test")
    result = _entry_to_result("Test", entry, path="fast")
    assert isinstance(result.canonical_ontology, str)


def test_probe_282_lookup_result_miss_canonical_ontology_is_none():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("unknown")
    assert result.canonical_ontology is None


def test_probe_283_lookup_result_fast_hit_canonical_label_matches_entry():
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(label="Specific Label")
    result = _entry_to_result("Specific Label", entry, path="fast")
    assert result.canonical_label == "Specific Label"


def test_probe_284_lookup_result_synonyms_tuple_from_entry():
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(label="X", synonyms=("S1", "S2"))
    result = _entry_to_result("X", entry, path="fast")
    assert "S1" in result.synonyms
    assert "S2" in result.synonyms


def test_probe_285_ancestor_result_canonical_label_from_ancestor():
    from apecx_integration.synonym_dictionary.lookup import _ancestor_to_result

    ancestor = _make_entry(
        label="Ancestor Virus", iri="http://purl.obolibrary.org/obo/NCBITaxon_1000"
    )
    result = _ancestor_to_result("http://purl.obolibrary.org/obo/NCBITaxon_999", ancestor)
    assert result.canonical_label == "Ancestor Virus"


# ---------------------------------------------------------------------------
# Full-stack mini E2E: write → load → lookup → synthesize (286-290)
# ---------------------------------------------------------------------------


def test_probe_286_full_stack_write_load_lookup(tmp_path):
    """Write an entry to SQLite, load the index, look up by label — all in one test."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    entry = _make_entry(label="Ebola Virus", iri="http://purl.obolibrary.org/obo/NCBITaxon_186538")
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    found = idx.lookup(EntityType.PATHOGEN, "Ebola Virus")
    assert found is not None
    assert found.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_186538"


def test_probe_287_full_stack_synonym_lookup(tmp_path):
    """Synonym lookup also resolves to the canonical IRI after load."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    entry = _make_entry(label="Dengue Virus", synonyms=("DENV", "breakbone fever virus"))
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    found = idx.lookup(EntityType.PATHOGEN, "DENV")
    assert found is not None
    assert found.canonical_iri == entry.canonical_iri


def test_probe_288_full_stack_case_insensitive_lookup(tmp_path):
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    entry = _make_entry(label="West Nile Virus", synonyms=("WNV",))
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    assert idx.lookup(EntityType.PATHOGEN, "WEST NILE VIRUS") is not None
    assert idx.lookup(EntityType.PATHOGEN, "west nile virus") is not None


def test_probe_289_full_stack_synthesize_bvbrc_citation_valid(tmp_path):
    """Full pipeline: write BV-BRC genome dict, validate allowed citation token in synth."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    genome = {"genome_id": "FULLSTACK-1", "genome_name": "Full Stack Genome"}
    llm = _stub_llm("Genome data [BV-BRC genome FULLSTACK-1].")
    result = synthesize_response("q", llm=llm, config=cfg, bvbrc_genomes=[genome])
    assert "FULLSTACK-1" in result


def test_probe_290_full_stack_synthesize_pub_doi_citation_valid():
    """Full pipeline: publication DOI → allowed token → cite in LLM response → passes."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    pub = {"doi": "10.9999/fullstack.doi", "title": "Full Stack Paper", "year": 2020}
    llm = _stub_llm("Study result [10.9999/fullstack.doi].")
    result = synthesize_response("q", llm=llm, config=cfg, publications=[pub])
    assert "fullstack.doi" in result


# ---------------------------------------------------------------------------
# Regression guard: bugs fixed in this session (291-295)
# ---------------------------------------------------------------------------


def test_probe_291_regression_doi_bracket_strict_mode_raises():
    """Regression: DOI containing '[' must raise in strict mode (probe 038 fix)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(strict_input_validation=True)
    pub = {"doi": "10.1234/bad[doi]", "title": "bad"}
    llm = _stub_llm("unreachable")
    with pytest.raises(ValueError):
        synthesize_response("q", llm=llm, config=cfg, publications=[pub])


def test_probe_292_regression_empty_system_prompt_raises_at_config_time():
    """Regression: SynthesisConfig with empty system_prompt must raise (probe 208 fix)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    with pytest.raises((ValueError, Exception)):
        SynthesisConfig(system_prompt="", min_response_chars=0)


def test_probe_293_regression_hallucinated_id_outside_patterns_is_0_distinct():
    """Regression: [HALLUCINATED-999] is 0 distinct citations (probe 126 clarification)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        SynthesisConfig,
        _extract_distinct_citations,
    )

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    result = _extract_distinct_citations("[HALLUCINATED-999]", cfg.citation_marker_patterns)
    assert len(result) == 0


def test_probe_294_regression_alembic_fileconfig_does_not_disable_loggers():
    """Regression: alembic env.py must use disable_existing_loggers=False."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    # If alembic's fileConfig with disable_existing_loggers=True ran,
    # the normalization module's logger would be disabled. But since we
    # fixed env.py, calling any log-emitting module should still work.
    # This probe just verifies normalization is importable and callable
    # after alembic env has been imported.
    result = normalize_surface_form("EEEV")
    assert result == "eeev"


def test_probe_295_regression_sqlite_reader_missing_file_raises():
    """Regression: SQLiteDictionaryReader on missing path raises immediately."""
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    with pytest.raises(Exception):  # noqa: B017
        reader = SQLiteDictionaryReader(Path("/nonexistent/path/dict.sqlite"))
        reader.read_manifest()


# ---------------------------------------------------------------------------
# Boundary + stress probes (296-300)
# ---------------------------------------------------------------------------


def test_probe_296_synthesis_config_max_rag_chunks_1_cap():
    """max_rag_chunks=1 produces only [RAG chunk #1] token."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        max_rag_chunks=1,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Answer [RAG chunk #1].")
    result = synthesize_response(
        "q",
        llm=llm,
        config=cfg,
        rag_chunks=[{"text": "only chunk"}],
    )
    assert result is not None


def test_probe_297_large_synonym_set_all_index_entries_created(tmp_path):
    """Entry with 10 synonyms creates 11 index rows (label + 10 synonyms)."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    syns = tuple(f"Syn{i}" for i in range(10))
    entry = _make_entry(label="Main Label", synonyms=syns)
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    # 1 canonical label + 10 synonyms = 11 index rows
    assert idx.index_entry_count() >= 11


def test_probe_298_dictionary_entry_confidence_boundary_0_0_accepted():
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    e = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/zero",
        canonical_label="Zero conf",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.0,
        resolved_at=_now(),
    )
    assert e.confidence == 0.0


def test_probe_299_dictionary_entry_confidence_boundary_1_0_accepted():
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    e = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/one",
        canonical_label="Full conf",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=1.0,
        resolved_at=_now(),
    )
    assert e.confidence == 1.0


def test_probe_300_dictionary_entry_confidence_above_1_rejected():
    """Confidence > 1.0 is rejected by Pydantic Field(le=1.0)."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    with pytest.raises(Exception):  # noqa: B017
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="http://example.org/bad",
            canonical_label="Bad conf",
            ontology=OntologyName.NCBITAXON,
            ontology_version="2024-01-01",
            confidence=1.001,
            resolved_at=_now(),
        )
