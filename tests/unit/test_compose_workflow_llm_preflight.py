"""#4 (2026-07-01) — compose_workflow refuses loudly when no apecx LLM is reachable.

Composition ALWAYS needs the apecx LLM (the composer drives its own LLM factory). With Ollama off
(the desktop default) the CP ``start_workflow`` used to die at the first compose call with a raw
ConnectError (composer ``max_retries: 0``) — an opaque traceback. compose_workflow now runs an
LLM-availability preflight (``llm_policy.resolve_llm``) and returns a structured, actionable error
BEFORE calling the control plane. The execute-only recall (run_id + execute=True) skips the preflight
(it needs no composer LLM). ``resolve_llm`` / ``get_client`` are injected so no live endpoint or CP
is required.
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.mcp_surface import llm_policy
from apecx_integration.mcp_surface.llm_policy import LlmResolution
from apecx_integration.mcp_surface.tools import workflows as wf


def test_compose_workflow_refuses_loudly_when_llm_unreachable(monkeypatch):
    calls: list = []

    class _StubClient:
        async def start_workflow(self, req):  # must NOT be reached
            calls.append(req)
            raise AssertionError("start_workflow must not be called when the LLM is unreachable")

    monkeypatch.setattr(wf, "get_client", lambda: _StubClient())
    monkeypatch.setattr(
        llm_policy,
        "resolve_llm",
        lambda locus, **k: LlmResolution(
            available=False, target=None, detail="no LLM: unreachable"
        ),
    )

    out = asyncio.run(wf.compose_workflow(description="make a thing", user_id="alex"))
    assert "error" in out and "apecx LLM" in out["error"]
    assert "APECX_LLM_BASE_URL" in out["hint"]
    assert out["detail"] == "no LLM: unreachable"
    assert calls == [], "the control plane must not be called after a failed preflight"


def test_compose_workflow_proceeds_past_preflight_when_llm_reachable(monkeypatch):
    class _Sentinel(Exception):
        pass

    class _StubClient:
        async def start_workflow(self, req):
            raise _Sentinel  # reaching here proves the preflight passed through

    monkeypatch.setattr(wf, "get_client", lambda: _StubClient())
    monkeypatch.setattr(
        llm_policy,
        "resolve_llm",
        lambda locus, **k: LlmResolution(available=True, target="ollama:m", detail="ok"),
    )

    with pytest.raises(_Sentinel):
        asyncio.run(wf.compose_workflow(description="make a thing", user_id="alex"))


def test_compose_workflow_needs_only_description_not_user_id(monkeypatch):
    """The MCP surface never supplies user_id — the sole required param is `description`. A call with
    only `description` (no user_id) must pass the guard and reach the composer, not raise."""

    class _Sentinel(Exception):
        pass

    class _StubClient:
        async def start_workflow(self, req):
            raise _Sentinel  # reaching here proves the user_id guard is gone

    monkeypatch.setattr(wf, "get_client", lambda: _StubClient())
    monkeypatch.setattr(
        llm_policy,
        "resolve_llm",
        lambda locus, **k: LlmResolution(available=True, target="ollama:m", detail="ok"),
    )

    with pytest.raises(_Sentinel):
        asyncio.run(wf.compose_workflow(description="count BV-BRC genomes"))  # NO user_id


def test_compose_workflow_still_requires_description(monkeypatch):
    """description remains the one genuinely-required param — an empty call is still a loud error."""
    monkeypatch.setattr(wf, "get_client", lambda: object())
    with pytest.raises(ValueError, match="provide `description`"):
        asyncio.run(wf.compose_workflow())


def test_compose_workflow_execute_only_recall_skips_llm_preflight(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("resolve_llm must not run on the execute-only recall")

    monkeypatch.setattr(llm_policy, "resolve_llm", _boom)

    class _Result:
        def model_dump(self, mode):
            return {"status": "ok"}

    class _StubClient:
        async def execute_workflow(self, req):
            return _Result()

    monkeypatch.setattr(wf, "get_client", lambda: _StubClient())

    out = asyncio.run(
        wf.compose_workflow(run_id="11111111-1111-4111-8111-111111111111", execute=True)
    )
    assert out["run_id"] == "11111111-1111-4111-8111-111111111111"
    assert out["execution"]["status"] == "ok"
