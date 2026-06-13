"""viral_epitope_evidence_review (Track D) — the evidence workflow as a catalog tool.

Proves the lightweight WorkflowBuilder catalog entry: builds with real child steps (guards the
0-child-steps silent failure), is registered + listed, gates missing params via RoC-2c, and —
gated on a reachable LLM + Globus — runs end-to-end through `run_workflow` to a real
WorkflowResult whose markdown ALWAYS carries a Structural evidence section.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import requests

pytestmark = pytest.mark.integration


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


def _llm_reachable() -> bool:
    """True only when the endpoint is up AND the configured chat model is actually
    available. A reachable endpoint with the model un-pulled must SKIP (honest), not
    fail — the model name is resolved exactly as ``build_chat_llm`` resolves it."""
    # Mirror _llm_factory.build_chat_llm's defaults exactly.
    base = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    model = os.environ.get("APECX_LLM_MODEL", "mistral-small:latest")
    stem = model.split(":", 1)[0]
    # The openai-compat base ends in /v1; Ollama's native model list is at the ROOT.
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base

    def _has_model(url: str, list_key: str, name_key: str) -> bool:
        try:
            r = requests.get(url, timeout=3)
            if not r.ok:
                return False
            names = [it.get(name_key, "") for it in r.json().get(list_key, [])]
            return any(n == model or n.split(":", 1)[0] == stem for n in names)
        except Exception:
            return False

    # Ollama-native (/api/tags) OR OpenAI-compat (/v1/models) — try both forms.
    return (
        _has_model(root + "/api/tags", "models", "name")
        or _has_model(root + "/v1/models", "data", "id")
        or _has_model(base + "/models", "data", "id")
    )


needs_llm_and_globus = pytest.mark.skipif(
    not (_llm_reachable() and _globus_reachable()),
    reason="needs a reachable LLM endpoint (APECX_LLM_*) AND Globus Search",
)

_QUERY = "conserved chikungunya structural polyprotein epitopes and structural references"


# --------------------------- no-network structural guards ---------------------------
def test_builder_produces_workflow_with_child_steps():
    """Guards the WorkflowBuilder 0-child-steps silent failure (loads with 0 steps → silent
    no_first_step). No network needed — construction only."""
    from apecx_integration.composition.workflows.viral_epitope_evidence_review.builder import (
        build_viral_epitope_evidence_review_workflow,
    )

    wf = build_viral_epitope_evidence_review_workflow()
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", None)
    assert isinstance(children, dict)
    assert set(children) == {"assemble", "structural", "review", "envelope"}


def test_registered_in_catalog_and_listed():
    from apecx_integration.mcp_surface.tools.discovery import list_workflows
    from apecx_integration.mcp_surface.workflow_registry import load_catalog

    names = {e.tool_name for e in load_catalog().workflows}
    assert "viral_epitope_evidence_review" in names

    out = asyncio.run(list_workflows())
    row = next(r for r in out["runnable"] if r["name"] == "viral_epitope_evidence_review")
    assert row["invoke_with"] == "run_workflow"
    assert isinstance(row["available"], bool)


def test_missing_query_returns_needs_input():
    """RoC-2c: missing required `query` → needs_input BEFORE any backend call."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_epitope_evidence_review", {"protein": "E1"}))
    assert out["status"] == "needs_input", out
    ct = out["control_transfer"]
    assert ct["reason"] == "missing_param"
    params = ct["next_action"]["params"]
    assert any(p["param_name"] == "query" and p["issue"] == "missing" for p in params)
    assert out["data_handle"] is None  # did not run


# --------------------------- end-to-end (real LLM + Globus) ---------------------------
@needs_llm_and_globus
def test_evidence_only_e2e_has_structural_section():
    """End-to-end against real Globus + LLM. RELIABILITY: status is always ok — even if
    the LLM's narrative fails the citation gate, the step degrades to a deterministic
    evidence summary rather than discarding the retrieved evidence."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_epitope_evidence_review", {"query": _QUERY}))
    assert out["status"] == "ok", out
    assert out["error"] is None
    assert out["run_id"]
    md = out["markdown"]
    assert md and md.strip()
    # The structural section is ALWAYS present — records or an explicit no-hit (no silent omission).
    assert "## Structural evidence" in md, md[:2000]


@needs_llm_and_globus
def test_structural_no_hit_is_named_e2e(monkeypatch):
    """With the Globus branch disabled, the structural leg MUST emit the loud no-hit line —
    proving the no-silent-failure path end-to-end (not just at the unit level)."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    # Disable the Globus branch so structural lookup deterministically finds nothing.
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_DISABLED", "1")
    out = asyncio.run(
        run_workflow("viral_epitope_evidence_review", {"query": "Mayaro virus nsP2 protease"})
    )
    # Degrade-loud guarantees a result; the structural section names the no-hit explicitly.
    assert out["status"] == "ok", out
    assert "No PDB or EMDB structural records" in out["markdown"], out["markdown"][:2000]
