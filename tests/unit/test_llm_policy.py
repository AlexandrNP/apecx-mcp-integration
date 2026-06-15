"""requires_llm gate (design §9): resolve-and-announce, or LOUDLY REFUSE before running.

The gate must (a) refuse an LLM-needing workflow when no LLM resolves — never null/strand;
(b) NOT refuse a desktop, final-synthesis-only workflow (it self-omits, needs no LLM); and
(c) announce the resolved LLM otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.mcp_surface import llm_policy
from apecx_integration.mcp_surface.llm_policy import resolve_llm, workflow_needs_llm_at_run


@pytest.fixture
def restore_locus():
    prior = get_active_locus()
    yield
    set_active_locus(prior)


def test_resolve_llm_unreachable_refuses_loud():
    res = resolve_llm(ExecutionLocus.AGENT, reachable=lambda _u: False)
    assert res.available is False
    assert res.target is None
    assert "needs an LLM but none is resolvable" in res.detail
    assert "APECX_LLM_BASE_URL" in res.detail  # actionable


def test_resolve_llm_reachable_announces():
    res = resolve_llm(ExecutionLocus.AGENT, reachable=lambda _u: True)
    assert res.available is True
    assert res.target and res.target.startswith("ollama:")
    assert "resolved to" in res.detail


def test_resolve_llm_external_needs_api_key(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.delenv("APECX_LLM_API_KEY", raising=False)
    assert resolve_llm(ExecutionLocus.AGENT).available is False
    monkeypatch.setenv("APECX_LLM_API_KEY", "sk-xyz")
    assert resolve_llm(ExecutionLocus.AGENT).available is True


def test_epitope_needs_llm_only_in_agent_locus():
    from apecx_integration.composition.workflows.viral_epitope_analysis.builder import (
        build_viral_epitope_analysis_workflow,
    )

    wf = build_viral_epitope_analysis_workflow()
    # Desktop: the only LLM step is final_synthesis → self-omits → no LLM needed.
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.DESKTOP) is False
    # Agent: that step synthesizes internally → needs the server LLM.
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.AGENT) is True


def test_rag_e2e_synthesis_needs_llm_only_in_agent_locus():
    """rag_e2e_synthesis is a discoverable PRODUCT workflow whose terminal RagSynthesisStep
    is final_synthesis — so a desktop user with no apecx LLM must NOT be refused it (the host
    synthesizes). In agent locus the step synthesizes internally → needs the server LLM."""
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(
        "src/apecx_integration/composition/workflows/rag_e2e_synthesis/"
        "rag_e2e_synthesis_workflow.yml"
    )
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.DESKTOP) is False
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.AGENT) is True


def test_deterministic_workflow_never_needs_llm():
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_workflow,
    )

    wf = build_viral_conserved_sites_workflow()
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.DESKTOP) is False
    assert workflow_needs_llm_at_run(wf, ExecutionLocus.AGENT) is False


def test_run_workflow_refuses_loud_when_llm_unavailable_in_agent_locus(monkeypatch, restore_locus):
    """AGENT locus + no resolvable LLM → run_workflow returns a loud refusal BEFORE running
    (no null/strand). The refusal carries the actionable detail and no run_id."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    set_active_locus(ExecutionLocus.AGENT)
    monkeypatch.setattr(
        llm_policy,
        "resolve_llm",
        lambda locus, **k: llm_policy.LlmResolution(
            available=False, target=None, detail="LOUD REFUSAL: no LLM (test)."
        ),
    )
    out = asyncio.run(run_workflow("viral_epitope_analysis", {"query": "chikv E1 epitopes"}))
    assert out["status"] == "error"
    assert "LOUD REFUSAL: no LLM (test)." in out["error"]
    assert not out.get("run_id")  # refused before running → no run recorded


def test_run_workflow_desktop_does_not_refuse_self_omitting_workflow(monkeypatch, restore_locus):
    """DESKTOP locus: the gate must NOT call resolve_llm for a final-synthesis-only workflow
    (it self-omits), so a desktop user with no LLM is NEVER refused for it."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    set_active_locus(ExecutionLocus.DESKTOP)

    def _must_not_resolve(locus, **k):
        raise AssertionError("resolve_llm called in desktop for a self-omitting workflow")

    monkeypatch.setattr(llm_policy, "resolve_llm", _must_not_resolve)
    # It will need real network to actually complete; we only assert it is NOT refused at the
    # gate (i.e. it proceeds past the gate — any later outcome is fine for this test).
    # _must_not_resolve raises if the gate calls resolve_llm — reaching here proves it did
    # not. The result may complete or error later (network), but it is NOT the gate refusal.
    out = asyncio.run(run_workflow("viral_epitope_analysis", {"query": "chikv E1"}))
    assert "none is resolvable" not in (out.get("error") or "")
