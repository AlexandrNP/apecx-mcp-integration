"""RHEA genomic-analysis is mandatory-but-degrade-loud in viral_epitope_analysis.

DEGRADE (RHEA unreachable): the workflow still completes (status=ok) — the MAFFT/structural/
literature legs have merit — but loudly DISCLOSES that RHEA is unavailable + how to fix it, and
the disclosure is STORED on the data_handle (rhea_conservation_note). It never silently drops the
RHEA section and never fails the run.

LIVE (a real rhea server): a real run computes a real MUSCLE alignment, surfaced in the report.

Both run the full real chain (real BV-BRC + MAFFT + LLM); decided on OUTPUT VALUES.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import urllib.request

import pytest

pytestmark = pytest.mark.integration

_QUERY = "chikungunya virus E1 conserved epitopes"


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        globus_sdk.SearchClient().post_search(
            "e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0}
        )
        return True
    except Exception:
        return False


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
        return True
    except Exception:
        return False


def _rhea_reachable() -> bool:
    if not os.environ.get("RHEA_MCP_URL") or importlib.util.find_spec("rhea") is None:
        return False
    try:
        urllib.request.urlopen(os.environ["RHEA_MCP_URL"], timeout=4)  # noqa: S310
        return True
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code != 500
    except Exception:
        return False


needs_globus = pytest.mark.skipif(not _globus_reachable(), reason="needs reachable Globus")
needs_ollama = pytest.mark.skipif(not _ollama_reachable(), reason="needs reachable Ollama")


@needs_globus
@needs_ollama
def test_rhea_unavailable_degrades_loud(monkeypatch):
    """RHEA unreachable -> the run still COMPLETES (status=ok) and loudly discloses the gap +
    the fix; the disclosure is stored on the data_handle. Never fails, never silently drops it."""
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    # Point RHEA at a dead port so its leg fails -> degrade-loud (regardless of any live server).
    monkeypatch.setenv("RHEA_MCP_URL", "http://localhost:9/mcp/")
    _clear_workflow_cache()
    out = asyncio.run(run_workflow("viral_epitope_analysis", {"query": _QUERY, "protein": "E1"}))

    assert out["status"] == "ok", out  # degrade-loud: NOT an error
    md = out["markdown"] or ""
    assert "RHEA genomic-analysis tools are NOT available" in md, md[:2000]
    assert "apecx-setup rhea" in md, md[:2000]  # fix instructions present
    # MANDATORY disclosure is STORED on the handle, not just rendered.
    parts = getattr(default_handle_store().get(out["data_handle"]), "parts", {}) or {}
    assert parts.get("rhea_conservation") is None, parts
    assert parts.get("rhea_conservation_note"), (
        "rhea_conservation_note must be stored on the handle"
    )


@pytest.mark.skipif(not _rhea_reachable(), reason="needs a LIVE rhea MCP server (apecx-setup rhea)")
@needs_globus
def test_rhea_live_real_muscle():
    """A real run with RHEA up computes a real MUSCLE alignment, surfaced in the report."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    _clear_workflow_cache()
    out = asyncio.run(run_workflow("viral_epitope_analysis", {"query": _QUERY, "protein": "E1"}))
    assert out["status"] == "ok", out
    md = out["markdown"] or ""
    assert "rhea_genomic_analysis" in md, md[:2000]
    assert "RHEA MUSCLE aligned" in md, md[:2000]
