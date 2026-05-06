"""Adversarial probes batch 7 — probes 181-210.

Targets: MCP canonical_entity tool shape and routing contracts,
_result_to_dict serialization invariants, entity_type filter mapping,
synthesize_response query edge cases, and SynthesisConfig field
interaction invariants.
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
    class _StubLLM:
        def invoke(self, messages):
            class _R:
                content = response

            return _R()

    return _StubLLM()


def _rag(text: str, *, chunk_id: str = "chunk-A", score: float = 0.9) -> dict[str, Any]:
    return {"id": chunk_id, "text": text, "score": score}


# ---------------------------------------------------------------------------
# _result_to_dict serialization (181-190)
# ---------------------------------------------------------------------------


def test_probe_181_result_to_dict_all_keys_present():
    """_result_to_dict produces all expected keys."""
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("test_entity", reason="unit test")
    d = _result_to_dict(result)
    expected_keys = {
        "surface_form",
        "resolution_path",
        "canonical_iri",
        "canonical_label",
        "canonical_ontology",
        "confidence",
        "resolution_status",
        "synonyms",
        "evidence",
    }
    assert expected_keys.issubset(d.keys())


def test_probe_182_result_to_dict_miss_canonical_iri_is_none():
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo"))
    assert d["canonical_iri"] is None


def test_probe_183_result_to_dict_miss_confidence_is_zero():
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo"))
    assert d["confidence"] == 0.0


def test_probe_184_result_to_dict_resolution_path_is_miss_for_miss():
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo"))
    assert d["resolution_path"] == "miss"


def test_probe_185_result_to_dict_synonyms_is_list_not_tuple():
    """synonyms is serialized as list (JSON-serializable), not tuple."""
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo"))
    assert isinstance(d["synonyms"], list)


def test_probe_186_result_to_dict_resolution_status_is_string():
    """resolution_status is a string value, not an enum object."""
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo"))
    assert isinstance(d["resolution_status"], str)
    assert d["resolution_status"] == "unresolved"


def test_probe_187_result_to_dict_surface_form_preserved():
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("SARS-CoV-2"))
    assert d["surface_form"] == "SARS-CoV-2"


def test_probe_188_result_to_dict_evidence_is_string():
    from apecx_integration.mcp_surface.tools.canonical_entity import _result_to_dict
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    d = _result_to_dict(fast_miss("foo", reason="because"))
    assert isinstance(d["evidence"], str)


def test_probe_189_entity_type_map_covers_four_types():
    """_ENTITY_TYPE_MAP has entries for pathogen, vaccine, disease, gene."""
    from apecx_integration.mcp_surface.tools.canonical_entity import _ENTITY_TYPE_MAP
    from apecx_integration.synonym_dictionary.enums import EntityType

    assert _ENTITY_TYPE_MAP.get("pathogen") == EntityType.PATHOGEN
    assert _ENTITY_TYPE_MAP.get("vaccine") == EntityType.VACCINE
    assert _ENTITY_TYPE_MAP.get("disease") == EntityType.DISEASE
    assert _ENTITY_TYPE_MAP.get("gene") == EntityType.GENE


def test_probe_190_entity_type_map_unknown_key_returns_none():
    from apecx_integration.mcp_surface.tools.canonical_entity import _ENTITY_TYPE_MAP

    assert _ENTITY_TYPE_MAP.get("unknown_entity_type_xyz") is None


# ---------------------------------------------------------------------------
# synthesize_response query edge cases (191-200)
# ---------------------------------------------------------------------------


def test_probe_191_empty_query_raises_before_llm():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Would not be reached.")
    with pytest.raises(ValueError, match="[Qq]uery|empty"):
        synthesize_response("", llm=llm, config=cfg)


def test_probe_192_whitespace_only_query_raises():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Would not be reached.")
    with pytest.raises(ValueError, match="[Qq]uery|empty"):
        synthesize_response("   \n\t  ", llm=llm, config=cfg)


def test_probe_193_non_string_query_raises():
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=False,
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )
    llm = _stub_llm("Would not be reached.")
    with pytest.raises((ValueError, TypeError)):
        synthesize_response(None, llm=llm, config=cfg)  # type: ignore[arg-type]


def test_probe_194_single_char_query_passes():
    """A single non-whitespace character is a valid query."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Answer [RAG chunk #1].")
    result = synthesize_response("q", llm=llm, config=cfg, rag_chunks=[_rag("data")])
    assert result is not None


def test_probe_195_query_leading_trailing_whitespace_stripped():
    """Query with leading/trailing whitespace is processed correctly."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        fail_on_empty_retrieval=True,
        require_inline_citations=True,
        min_distinct_citations=1,
        validate_citations_against_inputs=True,
    )
    llm = _stub_llm("Answer [RAG chunk #1].")
    result = synthesize_response(
        "   Zika virus infection   ", llm=llm, config=cfg, rag_chunks=[_rag("Zika data.")]
    )
    assert result is not None


def test_probe_196_llm_returns_none_raises():
    """When LLM returns None as content, synthesizer raises ValueError."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )

    class _NullLLM:
        def invoke(self, messages):
            class _R:
                content = None

            return _R()

    with pytest.raises(ValueError, match="[Ee]mpty|content|LLM"):
        synthesize_response(
            "q", llm=_NullLLM(), config=cfg, rag_chunks=[_rag("data", chunk_id="c1")]
        )


def test_probe_197_llm_returns_int_raises():
    """When LLM returns an int as content, synthesizer raises ValueError."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )

    class _IntLLM:
        def invoke(self, messages):
            class _R:
                content = 42

            return _R()

    with pytest.raises(ValueError, match="[Ee]mpty|content|LLM|non-string"):
        synthesize_response(
            "q", llm=_IntLLM(), config=cfg, rag_chunks=[_rag("data", chunk_id="c1")]
        )


def test_probe_198_llm_returns_whitespace_only_raises():
    """When LLM returns whitespace-only content, synthesizer raises ValueError."""
    from apecx_integration.agents.rag_synthesis.synthesizer import synthesize_response

    cfg = _stub_config(
        require_inline_citations=False,
        min_distinct_citations=0,
        validate_citations_against_inputs=False,
    )

    llm = _stub_llm("   \n   ")
    with pytest.raises(ValueError, match="[Ee]mpty|content|LLM"):
        synthesize_response("q", llm=llm, config=cfg, rag_chunks=[_rag("data", chunk_id="c1")])


def test_probe_199_config_none_uses_default_config():
    """When config=None, the default synthesis_config.yml is loaded successfully."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        SynthesisConfig,
        _load_default_config,
    )

    cfg = _load_default_config()
    assert isinstance(cfg, SynthesisConfig)


def test_probe_200_default_config_has_system_prompt():
    """The default config has a non-empty system_prompt."""
    from apecx_integration.agents.rag_synthesis.synthesizer import _load_default_config

    cfg = _load_default_config()
    assert isinstance(cfg.system_prompt, str)
    assert len(cfg.system_prompt.strip()) > 0


# ---------------------------------------------------------------------------
# SynthesisConfig field interaction invariants (201-210)
# ---------------------------------------------------------------------------


def test_probe_201_synthesis_config_max_rag_chunks_cap():
    """max_rag_chunks defaults to a positive int."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.max_rag_chunks >= 1


def test_probe_202_synthesis_config_max_bvbrc_genomes_cap():
    """max_bvbrc_genomes defaults to a positive int."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.max_bvbrc_genomes >= 1


def test_probe_203_synthesis_config_max_violin_mappings_cap():
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.max_violin_mappings >= 1


def test_probe_204_synthesis_config_max_publications_cap():
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.max_publications >= 1


def test_probe_205_synthesis_config_citation_marker_patterns_non_empty():
    """citation_marker_patterns has at least 4 patterns by default."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert len(cfg.citation_marker_patterns) >= 4


def test_probe_206_synthesis_config_custom_citation_patterns_override_default():
    """citation_marker_patterns can be overridden in config."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(
        system_prompt="x",
        min_response_chars=0,
        citation_marker_patterns=[r"\[CUSTOM-[0-9]+\]"],
    )
    assert cfg.citation_marker_patterns == [r"\[CUSTOM-[0-9]+\]"]


def test_probe_207_synthesis_config_custom_pattern_extraction_works():
    """A custom citation pattern is used by _extract_distinct_citations."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _extract_distinct_citations,
    )

    patterns = [r"\[CUSTOM-[0-9]+\]"]
    result = _extract_distinct_citations("See [CUSTOM-1] and [CUSTOM-42].", patterns)
    assert result == {"[CUSTOM-1]", "[CUSTOM-42]"}


def test_probe_208_synthesis_config_system_prompt_empty_raises():
    """A blank system_prompt should raise at config-load time."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    with pytest.raises((ValueError, Exception)):
        SynthesisConfig(system_prompt="", min_response_chars=0)


def test_probe_209_synthesis_config_fail_on_empty_retrieval_default_true():
    """fail_on_empty_retrieval is True by default (safe-fail posture)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.fail_on_empty_retrieval is True


def test_probe_210_synthesis_config_strict_input_validation_default_true():
    """strict_input_validation is True by default."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="x", min_response_chars=0)
    assert cfg.strict_input_validation is True
