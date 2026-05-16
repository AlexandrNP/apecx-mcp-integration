"""Unit tests for WebSearchContextStep — fake-backend tests gated on ``ddgs``.

The tests construct ``WebSearchContextStep`` which wraps nanobrain's
``WebSearchTool``. ``WebSearchTool._init_from_config`` instantiates
the configured backend at load time; the default is ``duckduckgo``
which lazy-imports ``ddgs`` FAIL-LOUD. So even with a "fake" test
backend swap-in, construction needs ``ddgs`` first.

For environments without ``ddgs``, the whole file skips cleanly. Same
pattern as nanobrain's ``tests/unit/test_globus_credentials.py`` for
``keyring``.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

pytest.importorskip(
    "ddgs",
    reason=(
        "ddgs not installed — WebSearchTool's default duckduckgo backend "
        "requires it. Install with `pip install ddgs`."
    ),
)

from nanobrain.core.component_base import ComponentConfigurationError
from nanobrain.library.tools.web_search import WebSearchBackend

from apecx_integration.composition.steps.web_search_context_step import (
    WebSearchContextStep,
)


class _FakeBackend(WebSearchBackend):
    """Canned-result backend; records calls; can be set to raise."""

    name = "fake"

    def __init__(self, results=None, raises: Exception | None = None):
        self._results = (
            results
            if results is not None
            else [
                {"title": "DESeq2 tutorial", "url": "http://x/1", "snippet": "how to run deseq2"},
                {"title": "scanpy docs", "url": "http://x/2", "snippet": "single cell analysis"},
            ]
        )
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, max_results: int):
        self.calls.append((query, max_results))
        if self._raises is not None:
            raise self._raises
        return list(self._results)


def _stage(tmp_path: Path, *, step_extras: str = "") -> WebSearchContextStep:
    """Write a tool YAML + step YAML and load the step via from_config."""
    tool_yml = tmp_path / "web_search_tool.yml"
    tool_yml.write_text(
        "name: web_search\n"
        "tool_type: external\n"
        "description: test web search\n"
        "parameters:\n"
        "  backend: duckduckgo\n"
        "  max_results: 5\n"
        "tool_card:\n"
        "  capabilities: ['web_search']\n"
    )
    step_yml = tmp_path / "web_search_context_step.yml"
    step_yml.write_text(
        "name: web_search_context_test\n"
        f"web_search_tool_config: '{tool_yml}'\n"
        "max_results: 3\n" + step_extras
    )
    return WebSearchContextStep.from_config(str(step_yml))


def _with_fake(step: WebSearchContextStep, fake: _FakeBackend) -> WebSearchContextStep:
    step._tool.backend = fake
    return step


# ---- construction ---------------------------------------------------------


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "web_search_context_test"


def test_missing_tool_config_fails_loud(tmp_path):
    step_yml = tmp_path / "step.yml"
    step_yml.write_text("name: bad\nweb_search_tool_config: '/nonexistent/tool.yml'\n")
    with pytest.raises(FileNotFoundError, match="web_search_tool_config"):
        WebSearchContextStep.from_config(str(step_yml))


def test_blank_tool_config_rejected(tmp_path):
    step_yml = tmp_path / "step.yml"
    step_yml.write_text("name: bad\nweb_search_tool_config: '  '\n")
    with pytest.raises(Exception, match="(?i)web_search_tool_config"):
        WebSearchContextStep.from_config(str(step_yml))


# ---- enrichment behavior --------------------------------------------------


def test_enriches_code_spec_with_results(tmp_path):
    step = _with_fake(_stage(tmp_path), _FakeBackend())
    out = asyncio.run(
        step.process(
            {"code_spec": "Run DESeq2 differential expression", "task_category": "bixbench"}
        )
    )
    assert out["websearch_hit"] is True
    assert out["websearch_results_used"] == 2
    assert "Relevant web context" in out["code_spec"]
    assert "DESeq2 tutorial" in out["code_spec"]
    assert "Run DESeq2 differential expression" in out["code_spec"]  # original preserved


def test_empty_results_passthrough(tmp_path):
    """Search ran, found nothing — NOT a failure; spec passes through unenriched."""
    step = _with_fake(_stage(tmp_path), _FakeBackend(results=[]))
    out = asyncio.run(step.process({"code_spec": "obscure problem", "task_category": "x"}))
    assert out["websearch_hit"] is False
    assert out["websearch_results_used"] == 0
    assert out["code_spec"] == "obscure problem"  # unchanged


def test_backend_failure_propagates_loud(tmp_path):
    """A backend fault must NOT silently degrade to 'no context' — it raises."""
    step = _with_fake(_stage(tmp_path), _FakeBackend(raises=RuntimeError("rate limited")))
    with pytest.raises(ComponentConfigurationError, match="rate limited"):
        asyncio.run(step.process({"code_spec": "anything", "task_category": "x"}))


def test_passthrough_fields_preserved(tmp_path):
    step = _with_fake(_stage(tmp_path), _FakeBackend())
    out = asyncio.run(
        step.process(
            {
                "code_spec": "do a thing",
                "task_category": "mbpp_math",
                "entry_point": "solve",
                "test_hint": "assert solve(1) == 2",
                "function_signature": "def solve(x):",
            }
        )
    )
    assert out["task_category"] == "mbpp_math"
    assert out["entry_point"] == "solve"
    assert out["test_hint"] == "assert solve(1) == 2"
    assert out["function_signature"] == "def solve(x):"


def test_trigger_envelope_unwrap(tmp_path):
    """A {<input_du>: {code_spec: ...}} envelope is unwrapped."""
    step = _with_fake(_stage(tmp_path), _FakeBackend())
    out = asyncio.run(
        step.process({"web_search_input": {"code_spec": "wrapped spec", "task_category": "x"}})
    )
    assert out["websearch_hit"] is True
    assert "wrapped spec" in out["code_spec"]


def test_empty_code_spec_fails_loud(tmp_path):
    step = _with_fake(_stage(tmp_path), _FakeBackend())
    with pytest.raises(ValueError, match="empty code_spec"):
        asyncio.run(step.process({"code_spec": "   ", "task_category": "x"}))


def test_query_derivation_uses_first_line_and_truncates(tmp_path):
    step = _with_fake(_stage(tmp_path, step_extras="max_query_chars: 20\n"), _FakeBackend())
    fake = step._tool.backend
    spec = "First line is the query that is quite long\nsecond line ignored"
    out = asyncio.run(step.process({"code_spec": spec, "task_category": "x"}))
    query, max_results = fake.calls[0]
    assert query == "First line is the qu"  # first line, truncated to 20 chars
    assert len(query) == 20
    assert max_results == 3  # from step config
    assert out["websearch_query"] == query


def test_from_cache_flag_passes_through(tmp_path):
    """The tool's from_cache flag surfaces on the step output.

    Uses the real tool with a cache dir + fake backend: first call
    live, second call from cache.
    """
    with tempfile.TemporaryDirectory() as cache_dir:
        # Rebuild the tool with a cache_dir by staging a fresh step.
        tool_yml = Path(tmp_path) / "cached_tool.yml"
        tool_yml.write_text(
            "name: web_search\ntool_type: external\ndescription: t\n"
            "parameters:\n  backend: duckduckgo\n"
            f"  cache_dir: '{cache_dir}'\n"
            "tool_card:\n  capabilities: ['web_search']\n"
        )
        step_yml = Path(tmp_path) / "cached_step.yml"
        step_yml.write_text(f"name: cached\nweb_search_tool_config: '{tool_yml}'\nmax_results: 2\n")
        cached_step = WebSearchContextStep.from_config(str(step_yml))
        cached_step._tool.backend = _FakeBackend()
        first = asyncio.run(cached_step.process({"code_spec": "q", "task_category": "x"}))
        second = asyncio.run(cached_step.process({"code_spec": "q", "task_category": "x"}))
        assert first["websearch_from_cache"] is False
        assert second["websearch_from_cache"] is True
