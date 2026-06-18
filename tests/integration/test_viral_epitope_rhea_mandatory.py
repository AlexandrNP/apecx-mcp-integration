"""RHEA is MANDATORY for viral_epitope_analysis (fail-closed).

T6 (deterministic, no network): with RHEA not configured, run_workflow refuses the run up
front and returns status=error with an actionable hint — never status=ok with a degrade note.

T7 (gated on a LIVE rhea server): a real run computes a real MUSCLE alignment, surfaced in the
report's `## Analysis steps` as the `rhea_genomic_analysis` stage. Decided on the OUTPUT VALUE.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import urllib.request

import pytest

pytestmark = pytest.mark.integration


def _rhea_reachable() -> bool:
    if not os.environ.get("RHEA_MCP_URL"):
        return False
    if importlib.util.find_spec("rhea") is None:
        return False
    try:
        urllib.request.urlopen(os.environ["RHEA_MCP_URL"], timeout=4)  # noqa: S310
        return True
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code != 500  # 4xx (e.g. 406) = server UP; 500 = backend down
    except Exception:
        return False


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        globus_sdk.SearchClient().post_search(
            "e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0}
        )
        return True
    except Exception:
        return False


def test_rhea_absent_errors(monkeypatch):
    """Fail-closed gate: RHEA absent -> status=error + hint, BEFORE any run (no network needed)."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    monkeypatch.delenv("RHEA_MCP_URL", raising=False)
    out = asyncio.run(
        run_workflow(
            "viral_epitope_analysis", {"query": "chikungunya E1 epitopes", "protein": "E1"}
        )
    )
    assert out["status"] == "error", out
    err = (out.get("error") or "").lower()
    assert "rhea" in err, out
    assert "rhea_mcp_url" in err or "apecx-setup rhea" in err, out
    # NOT a degrade-to-note success.
    assert "rhea_conservation_note" not in (out.get("markdown") or "")


@pytest.mark.skipif(not _rhea_reachable(), reason="needs a LIVE rhea MCP server (apecx-setup rhea)")
@pytest.mark.skipif(not _globus_reachable(), reason="needs reachable Globus for the upstream run")
def test_rhea_live_real_muscle():
    """A real run computes a real MUSCLE alignment, surfaced in the report's Analysis steps."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    _clear_workflow_cache()
    out = asyncio.run(
        run_workflow(
            "viral_epitope_analysis",
            {"query": "chikungunya virus E1 conserved epitopes", "protein": "E1"},
        )
    )
    assert out["status"] == "ok", out
    md = out["markdown"] or ""
    # The mandatory RHEA leg ran and its real MUSCLE result is in the report (decide on the value).
    assert "rhea_genomic_analysis" in md, md[:2000]
    assert "RHEA MUSCLE aligned" in md, md[:2000]
