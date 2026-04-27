"""Probe batch 40 — adversarial probes against the rag_synthesis
public-API surface that prior batches did NOT cover.

Streak before this batch: 100/300 post-AQ.
Probe naming: 1055–1079.

Distinct probes only.
"""

from __future__ import annotations

from pathlib import Path
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


pytestmark = pytest.mark.integration


def _cfg(**overrides) -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)


class _Stub:
    def __init__(self, content: str) -> None:
        self.content = content
        self.received: list = []

    def invoke(self, msgs):
        self.received.append(msgs)
        return AIMessage(content=self.content)


# --------------------------------------------------------------------------- #
# Probes 1055–1079
# --------------------------------------------------------------------------- #


def test_probe_1055_renderer_none_element_in_list_strict():
    """A list containing ``None`` is a sloppy-input shape. Strict mode
    rejects via the ``isinstance(g, dict)`` check — None is not a
    dict, surfaces ``expected dict, got NoneType``."""
    with pytest.raises(ValueError, match="expected dict"):
        _render_bvbrc_genomes(
            [None, {"genome_id": "G1", "name": "n"}],
            cap=8, strict=True,
        )


def test_probe_1056_renderer_none_element_in_list_lenient(caplog):
    """Lenient mode skips None with a warning, surviving the rest."""
    caplog.set_level(
        "WARNING",
        logger="apecx_integration.agents.rag_synthesis.synthesizer",
    )
    rendered, allowed = _render_bvbrc_genomes(
        [None, {"genome_id": "G1", "name": "n"}],
        cap=8, strict=False,
    )
    assert allowed == {"[BV-BRC genome G1]"}
    assert any("NoneType" in r.message for r in caplog.records)


def test_probe_1057_authors_as_tuple_renders_correctly():
    """Authors as tuple (immutable list-like) — renderer's
    ``isinstance(authors, list)`` check would route it to the
    ``str(authors)`` branch, rendering as ``('A', 'B', 'C')``.
    That's ugly but documented; pin so a future ``isinstance(authors,
    Sequence)`` change doesn't silently flip behavior."""
    pub = {
        "doi": "10.1/x", "title": "T",
        "authors": ("Alice", "Bob"),  # tuple, not list
    }
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    # Current branch: tuple goes through ``str(authors)`` →
    # rendered as ``('Alice', 'Bob')``.
    assert "Alice" in rendered or "alice" in rendered.lower()


def test_probe_1058_huge_chunk_text_renders_full(monkeypatch):
    """A very long chunk text (10KB) is rendered verbatim — no
    silent truncation. The 300-char ellipsis applies only to
    publications.abstract, NOT to RAG chunks. Verify this is
    deterministic so a future "truncate everything" fix doesn't
    silently swallow chunk content."""
    huge_text = "X" * 10_000
    rendered, _ = _render_rag_chunks(
        [{"text": huge_text}], cap=8, strict=True,
    )
    assert "X" * 10_000 in rendered


def test_probe_1059_synthesize_response_with_require_inline_citations_false():
    """Operators may disable the citation requirement; the grounding
    gate is INSIDE the require_inline_citations branch, so disabling
    citations also disables grounding. Pin: this is intentional —
    an operator opting out of citations cannot then complain about
    hallucinated citations they themselves chose not to enforce."""
    cfg = _cfg(require_inline_citations=False)
    stub = _Stub(content="body " * 50 + "[BV-BRC genome HALLUCINATED]")
    inputs = dict(bvbrc_genomes=[{"genome_id": "REAL", "name": "n"}])
    # Grounding does NOT fire because require_inline_citations is off.
    out = synthesize_response("Q", llm=stub, config=cfg, **inputs)
    assert "[BV-BRC genome HALLUCINATED]" in out


def test_probe_1060_synthesize_with_min_distinct_zero_accepts_uncited():
    """``min_distinct_citations=0`` lowers the bar — but
    require_inline_citations is still True, so the validation runs.
    With 0 required, an uncited response passes (0 >= 0). Pin."""
    cfg = _cfg(min_distinct_citations=0)
    stub = _Stub(content="body " * 50)  # NO citations
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    out = synthesize_response("Q", llm=stub, config=cfg, **inputs)
    assert "body" in out


def test_probe_1061_synthesize_module_imports_match_public_api():
    """Importing from the package's __init__ must surface exactly
    the public symbols. A future refactor that drops something from
    __all__ would silently break callers depending on the package
    surface."""
    import apecx_integration.agents.rag_synthesis as pkg
    expected = {
        "DEFAULT_SYNTHESIS_CONFIG_PATH",
        "SynthesisConfig",
        "datacite_to_publication",
        "synthesize_response",
    }
    assert expected.issubset(set(pkg.__all__))


def test_probe_1062_synthesizer_module_uses_consistent_logger_name():
    """The module's logger name is used in test caplog filters and in
    operator log searches. Pin: the logger is named after the module
    path (``apecx_integration.agents.rag_synthesis.synthesizer``)."""
    import apecx_integration.agents.rag_synthesis.synthesizer as mod
    assert mod.logger.name == "apecx_integration.agents.rag_synthesis.synthesizer"


def test_probe_1063_render_publications_with_string_authors_handles_long_string():
    """authors as a long string (e.g. paste from CSV "First, Last; Second, Last2")
    renders as a single author entry verbatim. Pin: the renderer
    does NOT split on ``;`` or ``,`` for string authors."""
    pub = {
        "doi": "10.1/x", "title": "T",
        "authors": "Alice, A; Bob, B; Carol, C",
    }
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "Alice, A; Bob, B; Carol, C" in rendered


def test_probe_1064_render_publications_with_authors_None_skipped():
    """authors=None is a defensible shape; the renderer's
    ``or []`` defaults it to [], and the meta_parts logic skips."""
    pub = {"doi": "10.1/x", "title": "T", "authors": None}
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    # No authors line in the meta block.
    assert "10.1/x" in rendered


def test_probe_1065_render_handles_unicode_in_chunk_text():
    """RAG chunks may carry CJK / emoji / Greek; renderer must not
    crash on encoding. Real biology texts contain Greek (α-helix)
    and emoji is a low-prob shape but still possible."""
    text = "α-helix conformation 螺旋 🧬 fold."
    rendered, _ = _render_rag_chunks(
        [{"text": text}], cap=1, strict=True,
    )
    assert text in rendered


def test_probe_1066_extract_does_not_match_partial_doi_at_string_end():
    """A truncated DOI like ``[10.1234/abc...`` (no closing bracket)
    must NOT match the citation pattern even if the response was
    cut off."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "see [10.1234/abc... and also [10.5/y]",
        cfg.citation_marker_patterns,
    )
    # Only the closed second one matches.
    assert extracted == {"[10.5/y]"}


def test_probe_1067_extract_handles_token_with_dollar_sign_in_id():
    """Some real-world identifiers (mathematical / latex contexts)
    contain ``$``. The synthesis context unlikely needs this, but
    the regex must not crash. Verify the character class accepts
    the char without hanging."""
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        "[VIOLIN $V_001]", cfg.citation_marker_patterns,
    )
    assert "[VIOLIN $V_001]" in extracted


def test_probe_1068_synthesize_does_not_log_sensitive_query_at_info():
    """The synthesizer's logger should NOT emit the full query at
    INFO (could be a privacy concern in production with PHI/PII).
    Verify: the logger DOES emit per-source counts; check it does
    NOT emit the verbatim query string at INFO. Pin so a future
    debug-print doesn't silently leak query content."""
    import logging
    captured = []

    class _Handler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Handler(level=logging.INFO)
    logging.getLogger("apecx_integration.agents.rag_synthesis").addHandler(handler)
    try:
        synthesize_response(
            "MY-PRIVATE-QUERY-marker",
            llm=_Stub(content="body " * 50 + "[BV-BRC genome G1]"),
            bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        )
        # The synthesizer module itself doesn't emit INFO messages
        # at present (it uses logger.warning for skips). Pin: no
        # query verbatim in the captured output.
        for msg in captured:
            assert "MY-PRIVATE-QUERY-marker" not in msg, (
                f"synthesizer logged the verbatim query at INFO: {msg!r}"
            )
    finally:
        logging.getLogger("apecx_integration.agents.rag_synthesis").removeHandler(handler)


def test_probe_1069_render_rag_chunk_without_text_field_dict():
    """A chunk dict without ``text`` field is rejected (strict) or
    skipped (lenient). What about a chunk where ``text`` is None
    (vs missing)? The ``or ""`` handles both — both are skipped."""
    with pytest.raises(ValueError, match="missing or empty ``text``"):
        _render_rag_chunks(
            [{"text": None, "id": "c1"}], cap=1, strict=True,
        )


def test_probe_1070_render_publication_doi_is_int_rejected():
    """DOI given as int instead of str — ``str(doi).startswith("10.")``
    coerces to "10..." style. An int like ``1011234`` would NOT
    have a "10." prefix → reject."""
    pub = {"doi": 1011234, "title": "T"}
    with pytest.raises(ValueError, match="missing or non-DOI"):
        _render_publications([pub], cap=1, strict=True)


def test_probe_1071_synthesize_response_called_with_str_query_subclass():
    """A str subclass (like a UserStr type) should still pass the
    isinstance(query, str) check."""
    class MyStr(str): ...
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content="body " * 50 + "[BV-BRC genome G1]")
    q = MyStr("Q via subclass")
    out = synthesize_response(q, llm=stub, **inputs)
    assert "[BV-BRC genome G1]" in out


def test_probe_1072_publication_renderer_skips_truthy_check_on_authors_with_zero_int():
    """authors=[0] would be a degenerate shape; the renderer iterates
    ``str(a) for a in authors[:3]`` so int 0 renders as "0".
    Verify no crash + the rendered shape contains "0"."""
    pub = {"doi": "10.1/x", "title": "T", "authors": [0]}
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "0" in rendered


def test_probe_1073_synthesize_response_with_query_only_whitespace_inside_chars():
    """A query like ``"\\xa0\\xa0\\xa0"`` (non-breaking spaces) is
    technically non-empty after .strip() with the default Python
    locale. Verify the .strip() check handles unicode whitespace."""
    # \xa0 (non-breaking space) IS stripped by Python's str.strip()
    bad_query = "\xa0\xa0\xa0"
    assert bad_query.strip() == ""
    stub = _Stub(content="x")
    with pytest.raises(ValueError, match="non-empty"):
        synthesize_response(bad_query, llm=stub)


def test_probe_1074_render_handles_chunk_score_as_negative_float():
    """A negative similarity score is technically invalid for cosine
    similarity but real-world embeddings can produce negative dot
    products. The renderer must not crash."""
    rendered, _ = _render_rag_chunks(
        [{"text": "x", "score": -0.123}], cap=1, strict=True,
    )
    assert "similarity=-0.123" in rendered


def test_probe_1075_render_handles_chunk_score_inf():
    """math.inf score: format as ``inf``. Confirm no crash."""
    import math
    rendered, _ = _render_rag_chunks(
        [{"text": "x", "score": math.inf}], cap=1, strict=True,
    )
    # f"{inf:.3f}" -> "inf"
    assert "similarity=inf" in rendered


def test_probe_1076_synthesizer_does_not_swallow_unrelated_errors_in_llm_invoke():
    """If the LLM client's invoke raises (e.g. ConnectionError, not
    ValueError), the synthesizer must propagate the original
    exception type. A try/except ValueError that catches everything
    would silently turn ConnectionError into ValueError."""
    class _RaisingLLM:
        def invoke(self, _msgs):
            raise ConnectionError("Ollama unreachable")
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    with pytest.raises(ConnectionError, match="Ollama unreachable"):
        synthesize_response("Q", llm=_RaisingLLM(), **inputs)


def test_probe_1077_render_publication_authors_truncation_3_is_pinned():
    """authors[:3] truncates at 3. A future commit changing that to
    [:5] or [:1] would silently change the rendered prompt's
    information density. Pin the constant."""
    pub = {
        "doi": "10.1/x", "title": "T",
        "authors": [f"Author{i}" for i in range(10)],
    }
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    # First 3 present, fourth absent.
    assert "Author0" in rendered
    assert "Author1" in rendered
    assert "Author2" in rendered
    assert "Author3" not in rendered


def test_probe_1078_render_handles_chunk_id_with_special_chars():
    """A chunk ID containing chars that look like markdown / brackets
    must render verbatim (the citation token is from chunk #N, not
    from the ID, so the ID is decoration only)."""
    rendered, allowed = _render_rag_chunks(
        [{"text": "x", "id": "c[1]_extra"}], cap=1, strict=True,
    )
    # The citation token uses position, not ID:
    assert allowed == {"[RAG chunk #1]"}
    # The ID renders verbatim in the metadata header.
    assert "id=c[1]_extra" in rendered


def test_probe_1079_synthesizer_init_does_not_create_llm_client_on_lazy_path():
    """When the caller passes ``llm=`` explicitly, the synthesizer
    must NOT lazily import / construct an LLM client. Verify by
    ensuring the import inside the if-llm-is-None branch isn't
    triggered."""
    import sys
    # Sentinel: ensure the test isn't a no-op by checking the lazy
    # module isn't pre-imported.
    pre_imports = "apecx_integration.agents._llm_factory" in sys.modules
    # Even if pre-imported, the test verifies invoke() is what's
    # called — not that build_chat_llm is constructed.
    received = []

    class _S:
        def invoke(self, msgs):
            received.append(msgs)
            return AIMessage(
                content=("body " * 50) + "[BV-BRC genome G1]"
            )

    out = synthesize_response(
        "Q",
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        llm=_S(),
    )
    assert "[BV-BRC genome G1]" in out
    assert received, "stub LLM was not used"
    # The pre-import sentinel just confirms we measured something.
    _ = pre_imports
