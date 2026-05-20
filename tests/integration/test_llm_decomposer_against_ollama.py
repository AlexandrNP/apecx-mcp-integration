"""Ollama-gated integration test for LLMTaskDecomposer (EO-20 decomposer, real LLM).

Auto-skips when Ollama is unreachable (per the repo's live-LLM test convention). Asserts the
REAL path runs and returns a valid shape — NOT specific decomposition content (local-model
output varies; asserting a count would be flaky).
"""

from __future__ import annotations

import urllib.request

import pytest

from apecx_integration.composition.decomposition.llm_decomposer import LLMTaskDecomposer
from apecx_integration.composition.decomposition.local_decomposer import Task


def _ollama_up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="Ollama not reachable on localhost:11434")
@pytest.mark.asyncio
async def test_real_decompose_returns_valid_shape(monkeypatch):
    # Use a pulled chat model (the build_chat_llm default mistral-small may not be pulled).
    monkeypatch.setenv("APECX_LLM_MODEL", "mistral-nemo:latest")
    dec = LLMTaskDecomposer()
    subs = await dec.decompose(
        Task(
            "Find known epitopes for a viral surface protein and rank them by "
            "the strength of published experimental evidence."
        )
    )
    # Real path completed, response parsed, returned a valid list of Tasks (possibly empty —
    # the model may judge it atomic; both are valid). The point is: no raise, valid shape.
    assert isinstance(subs, list)
    assert all(isinstance(t, Task) and t.description.strip() for t in subs)
