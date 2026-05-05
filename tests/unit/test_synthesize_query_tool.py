"""Unit tests for the ``synthesize_query`` MCP tool.

Covers the input-validation + cached-loader + error-marshaling paths.
Real FAISS/LLM integration is exercised by
tests/integration/test_rag_e2e_pipeline.py — this file pins the tool's
contract WITHOUT real index/network/LLM dependencies.
"""

from __future__ import annotations

import pytest

import apecx_integration.mcp_surface.tools.synthesis as synth_tool


@pytest.fixture(autouse=True)
def reset_loader_state():
    """Reset the module-level singleton + error cache before each test."""
    synth_tool._ASSEMBLY_STEP = None
    synth_tool._SYNTHESIS_STEP = None
    synth_tool._LOAD_ERROR = None
    yield
    synth_tool._ASSEMBLY_STEP = None
    synth_tool._SYNTHESIS_STEP = None
    synth_tool._LOAD_ERROR = None


async def test_empty_query_returns_error_without_loading_steps():
    """Empty / whitespace queries short-circuit before step loading.

    Detection signal: a future refactor that loads steps eagerly (e.g.
    moves _load_steps() to the top of the function) makes this test
    fail because we'd see a load-error message rather than the input-
    validation message.
    """
    result = await synth_tool.synthesize_query("")
    assert "error" in result
    assert "non-empty" in result["error"]
    # _load_steps was NOT called: singleton is still None and no error
    # cached.
    assert synth_tool._ASSEMBLY_STEP is None
    assert synth_tool._LOAD_ERROR is None


async def test_whitespace_query_returns_error():
    result = await synth_tool.synthesize_query("   \t\n  ")
    assert "error" in result


async def test_load_error_surfaces_to_caller(monkeypatch, tmp_path):
    """When the workflow YAMLs are missing, the error message is
    descriptive (not a bare KeyError or NoneType exception).
    """

    def _fake_workflow_dir():
        # Point at a directory that doesn't have the expected YAMLs.
        return tmp_path / "nonexistent_workflow"

    monkeypatch.setattr(synth_tool, "_workflow_dir", _fake_workflow_dir)

    result = await synth_tool.synthesize_query("Why does this fail?")
    assert "error" in result
    assert "synthesis_context_assembly.yml" in result["error"]
    assert "not found" in result["error"]


async def test_load_error_is_cached(monkeypatch, tmp_path):
    """A failed load is recorded so subsequent calls don't repeatedly
    try to load broken YAMLs (turning a recoverable boot mistake into
    a per-request 5xx is wrong)."""

    monkeypatch.setattr(
        synth_tool,
        "_workflow_dir",
        lambda: tmp_path / "missing",
    )

    first = await synth_tool.synthesize_query("first call")
    assert "error" in first
    assert synth_tool._LOAD_ERROR is not None
    cached_error = synth_tool._LOAD_ERROR

    second = await synth_tool.synthesize_query("second call")
    assert "error" in second
    # Same cached error returned.
    assert second["error"] == first["error"]
    assert synth_tool._LOAD_ERROR is cached_error


async def test_synthesizer_value_error_is_marshaled_as_gate_failure(monkeypatch):
    """A ValueError from the synthesizer (e.g. fail_on_empty_retrieval
    fires) is returned as a structured error message — NOT raised."""

    class _FakeAssembly:
        _skip_pubmed = False

        async def process(self, _input):
            return {
                "query": "x",
                "rag_chunks": [],
                "violin_mappings": [],
                "bvbrc_genomes": [],
                "publications": [],
            }

    class _FakeSynthesis:
        async def process(self, _bundle):
            raise ValueError("fail_on_empty_retrieval gate fired: nothing matched")

    synth_tool._ASSEMBLY_STEP = _FakeAssembly()
    synth_tool._SYNTHESIS_STEP = _FakeSynthesis()

    result = await synth_tool.synthesize_query("any query")
    assert "error" in result
    assert "synthesis gate failed" in result["error"]
    assert "fail_on_empty_retrieval" in result["error"]


async def test_happy_path_returns_synthesis_and_retrieval_counts():
    """When both steps succeed, return the markdown plus a count
    summary so the model + operator can see what fed the synthesis."""

    class _FakeAssembly:
        _skip_pubmed = False

        async def process(self, _input):
            return {
                "query": "test",
                "rag_chunks": [{"id": 1}, {"id": 2}],
                "violin_mappings": [{"synonym_id": "VIOLIN_pathogen_1"}],
                "bvbrc_genomes": [],
                "publications": [{"doi": "10.1/a"}, {"doi": "10.1/b"}, {"doi": "10.1/c"}],
            }

    class _FakeSynthesis:
        async def process(self, _bundle):
            return {"synthesis": "# Answer\n\nGrounded text [1]"}

    synth_tool._ASSEMBLY_STEP = _FakeAssembly()
    synth_tool._SYNTHESIS_STEP = _FakeSynthesis()

    result = await synth_tool.synthesize_query("ok query")
    assert "error" not in result
    assert result["synthesis"].startswith("# Answer")
    assert result["retrieved"] == {
        "rag_chunks": 2,
        "violin_mappings": 1,
        "bvbrc_genomes": 0,
        "publications": 3,
    }


async def test_skip_pubmed_override_is_restored_after_call():
    """The cached assembly step's _skip_pubmed flag is mutated for the
    duration of one call and restored afterwards — otherwise a single
    call with skip_pubmed=True would silently disable PubMed for every
    subsequent call.
    """

    class _FakeAssembly:
        _skip_pubmed = False

        def __init__(self):
            self.observed_during_call: bool | None = None

        async def process(self, _input):
            self.observed_during_call = self._skip_pubmed
            return {
                "query": "x",
                "rag_chunks": [],
                "violin_mappings": [],
                "bvbrc_genomes": [],
                "publications": [],
            }

    class _FakeSynthesis:
        async def process(self, _bundle):
            return {"synthesis": "ok"}

    fake_assembly = _FakeAssembly()
    synth_tool._ASSEMBLY_STEP = fake_assembly
    synth_tool._SYNTHESIS_STEP = _FakeSynthesis()

    assert fake_assembly._skip_pubmed is False
    await synth_tool.synthesize_query("q", skip_pubmed=True)

    assert fake_assembly.observed_during_call is True
    # Restored after the call returns.
    assert fake_assembly._skip_pubmed is False
