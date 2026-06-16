"""Unit tests for the Globus Search integration.

Covers:
  - The stateless ``apecx_integration.agents.globus_search.search``
    client (with the SDK mocked / monkeypatched).
  - The ``query_globus_search`` MCP tool (input validation, error
    marshaling, happy path).
  - The synthesizer's ``_render_globus_results`` function (the prompt
    rendering + citation token contract).

Real Globus Search API calls are exercised by integration tests
(network-gated). Unit-mock parity rule (CLAUDE.md): every behavior we
mock here has — or needs — a matching integration test that hits the
real index.
"""

from __future__ import annotations

import pytest

import apecx_integration.agents.globus_search.client as gs_client
import apecx_integration.mcp_surface.tools.globus_search as gs_tool
from apecx_integration.agents.globus_search import (
    APECX_GLOBUS_INDEX_UUID,
    GlobusSearchUnavailableError,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    SynthesisConfig,
    _render_globus_results,
)

# ---------------------------------------------------------------------
# Globus Search client
# ---------------------------------------------------------------------


def test_search_empty_query_short_circuits_without_sdk_call(monkeypatch):
    """Empty / whitespace queries return ``[]`` without instantiating
    the SearchClient. Defends against accidental network calls in
    test fixtures that pass through empty strings."""
    called = {"flag": False}

    class _ExplodingClient:
        def post_search(self, *a, **kw):
            called["flag"] = True
            raise AssertionError("post_search should not be called")

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _ExplodingClient())

    assert gs_client.search("") == []
    assert gs_client.search("   ") == []
    assert called["flag"] is False


def test_search_disabled_env_var_short_circuits(monkeypatch):
    """``APECX_GLOBUS_SEARCH_DISABLED=1`` means: do not hit the network
    and do not instantiate the SDK client. For sandboxed CI."""
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_DISABLED", "1")

    def _explode(*a, **kw):
        raise AssertionError("SearchClient should not be constructed")

    monkeypatch.setattr("globus_sdk.SearchClient", _explode)
    assert gs_client.search("anything") == []


def test_search_handles_sdk_network_error(monkeypatch):
    """Network/auth errors from the SDK become
    ``GlobusSearchUnavailableError`` so the synthesis pipeline's
    ``asyncio.gather(return_exceptions=True)`` catches and degrades
    to an empty list rather than crashing the whole call."""

    class _FailingClient:
        def post_search(self, *a, **kw):
            raise ConnectionError("network down")

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _FailingClient())

    with pytest.raises(GlobusSearchUnavailableError) as exc_info:
        gs_client.search("EEEV vaccines")
    assert "network down" in str(exc_info.value)


def test_search_normalizes_gmeta_response(monkeypatch):
    """Globus returns ``gmeta`` entries with nested ``entries[*].content``;
    the client flattens to ``{subject, content, score}``."""

    class _StubClient:
        def post_search(self, index_uuid, body):
            assert index_uuid == APECX_GLOBUS_INDEX_UUID
            assert body["q"] == "EEEV"
            assert body["limit"] == 5
            return {
                "total": 100,
                "gmeta": [
                    {
                        "subject": "10.1234/abc",
                        "entries": [{"content": {"title": "EEEV paper"}}],
                    },
                    {
                        "subject": "PMID:12345",
                        "entries": [{"content": {"title": "Other"}}],
                    },
                ],
            }

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _StubClient())
    hits = gs_client.search("EEEV", max_results=5)
    assert len(hits) == 2
    assert hits[0]["subject"] == "10.1234/abc"
    assert hits[0]["content"]["title"] == "EEEV paper"
    assert hits[1]["subject"] == "PMID:12345"


def test_search_per_request_limit_capped_at_100(monkeypatch):
    """Globus enforces a 100-result cap PER REQUEST; the client never asks for more
    in a single page (larger totals are satisfied by paging, not a silent clamp)."""
    captured: dict = {}

    class _CapturingClient:
        def post_search(self, index_uuid, body):
            captured.update(body)
            return {"gmeta": [], "total": 0}

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _CapturingClient())
    gs_client.search("X", max_results=999)
    assert captured["limit"] == 100


def _corpus_stub(total: int):
    """A SearchClient stub serving ``total`` synthetic records, honoring offset/limit."""

    class _Paged:
        def post_search(self, index_uuid, body):
            off, lim = body["offset"], body["limit"]
            page = [
                {"subject": f"rec:{i}", "entries": [{"content": {"i": i}}]}
                for i in range(off, min(off + lim, total))
            ]
            return {"total": total, "gmeta": page}

    return _Paged()


def test_search_pages_until_max_results(monkeypatch):
    """A positive max_results above 100 is satisfied by PAGING, not a 100-clamp."""
    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _corpus_stub(500))
    hits = gs_client.search("X", max_results=250)
    assert len(hits) == 250
    assert hits[0]["subject"] == "rec:0"
    assert hits[-1]["subject"] == "rec:249"


def test_search_unbounded_retrieves_everything(monkeypatch):
    """max_results <= 0 → no limit: page the whole index."""
    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _corpus_stub(350))
    hits = gs_client.search("X", max_results=0)
    assert len(hits) == 350


def test_search_unbounded_truncates_at_ceiling_loud(monkeypatch, caplog):
    """Beyond Globus's 10000 deep-paging ceiling, stop + WARN (never a silent cap)."""
    import logging

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _corpus_stub(10500))
    with caplog.at_level(logging.WARNING):
        hits = gs_client.search("X", max_results=0)
    assert len(hits) == 10000
    assert any("truncated" in r.message.lower() for r in caplog.records)


def test_search_index_uuid_env_override(monkeypatch):
    """``APECX_GLOBUS_SEARCH_INDEX_UUID`` overrides the default UUID."""
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_INDEX_UUID", "deadbeef-1234-5678-90ab-cdef00000000")
    captured: dict = {}

    class _CapturingClient:
        def post_search(self, index_uuid, body):
            captured["uuid"] = index_uuid
            return {"gmeta": [], "total": 0}

    monkeypatch.setattr("globus_sdk.SearchClient", lambda *a, **kw: _CapturingClient())
    gs_client.search("X")
    assert captured["uuid"] == "deadbeef-1234-5678-90ab-cdef00000000"


# ---------------------------------------------------------------------
# query_globus_search MCP tool
# ---------------------------------------------------------------------


async def test_tool_empty_query_returns_error():
    out = await gs_tool.query_globus_search("")
    assert "error" in out
    assert "non-empty" in out["error"]


async def test_tool_marshals_unavailable_error(monkeypatch):
    """SDK / network failures surface as a structured error dict, not
    a raised exception. The MCP transport never sees the exception."""

    def _fail(*a, **kw):
        raise GlobusSearchUnavailableError("simulated outage")

    monkeypatch.setattr(gs_tool, "_search", _fail)
    out = await gs_tool.query_globus_search("EEEV")
    assert "error" in out
    assert "simulated outage" in out["error"]
    assert out["query"] == "EEEV"


async def test_tool_happy_path_returns_results_and_count(monkeypatch):
    fake_hits = [
        {"subject": "10.1/a", "content": {"title": "One"}, "score": 0.9},
        {"subject": "10.2/b", "content": {"title": "Two"}, "score": 0.8},
    ]
    monkeypatch.setattr(gs_tool, "_search", lambda q, **kw: fake_hits)

    out = await gs_tool.query_globus_search("EEEV", max_results=5)
    assert out["count"] == 2
    assert out["results"] == fake_hits
    assert out["query"] == "EEEV"


async def test_tool_unexpected_error_marshaled(monkeypatch):
    """Even non-GlobusSearchUnavailableError exceptions are caught."""

    def _explode(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(gs_tool, "_search", _explode)
    out = await gs_tool.query_globus_search("X")
    assert "error" in out
    assert "RuntimeError" in out["error"]


# ---------------------------------------------------------------------
# Synthesizer renderer
# ---------------------------------------------------------------------


def test_render_globus_empty_returns_placeholder():
    out, allowed = _render_globus_results([], cap=10, strict=True)
    assert "(no Globus" in out
    assert allowed == set()


def test_render_globus_cap_zero_skips_section():
    out, allowed = _render_globus_results(
        [{"subject": "X", "content": {"title": "T"}}],
        cap=0,
        strict=True,
    )
    assert "(no Globus" in out
    assert allowed == set()


def test_render_globus_creates_inline_citation_token():
    """Rendered hit produces a ``[Globus <subject>]`` citation token in
    the allowed-tokens set so the LLM can cite it without tripping the
    grounded-citation gate."""
    hits = [{"subject": "10.1234/abc", "content": {"title": "Test paper"}}]
    out, allowed = _render_globus_results(hits, cap=5, strict=True)
    assert "[Globus 10.1234/abc]" in allowed
    assert "Test paper" in out
    assert "10.1234/abc" in out


def test_render_globus_caps_to_n_hits():
    hits = [{"subject": f"id_{i}", "content": {}} for i in range(10)]
    out, allowed = _render_globus_results(hits, cap=3, strict=True)
    # Only first 3 produce tokens.
    assert len(allowed) == 3
    assert "[Globus id_0]" in allowed
    assert "[Globus id_5]" not in allowed


def test_render_globus_strict_rejects_missing_subject():
    """Strict mode raises on a hit without a subject (since the
    citation token can't be formed)."""
    with pytest.raises(ValueError):
        _render_globus_results(
            [{"content": {"title": "no subject"}}],
            cap=5,
            strict=True,
        )


def test_render_globus_strict_rejects_subject_with_brackets():
    """Strict mode rejects subjects whose characters break the citation
    extraction regex (``[``, ``]``, whitespace)."""
    with pytest.raises(ValueError):
        _render_globus_results(
            [{"subject": "weird] id", "content": {}}],
            cap=5,
            strict=True,
        )


def test_render_globus_lenient_skips_bad_hits(caplog):
    """Lenient mode (used by the live pipeline so a single bad row
    doesn't poison the whole prompt) skips bad hits with a WARNING
    instead of raising."""
    with caplog.at_level("WARNING"):
        out, allowed = _render_globus_results(
            [
                {"subject": None, "content": {}},
                {"subject": "valid_id", "content": {"title": "ok"}},
            ],
            cap=5,
            strict=False,
        )
    assert allowed == {"[Globus valid_id]"}
    assert "[Globus valid_id]" in out


def test_synthesis_config_max_globus_results_default():
    cfg = SynthesisConfig(system_prompt="dummy")
    assert cfg.max_globus_results == 10


def test_synthesis_config_globus_citation_pattern_present():
    cfg = SynthesisConfig(system_prompt="dummy")
    # The new pattern matches our token shape.
    assert any("Globus" in p for p in cfg.citation_marker_patterns)
