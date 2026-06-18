"""The per-run artifacts seam is workflow-agnostic — a NON-epitope workflow gets the full
artifact folder for free (report.md + data.json + data-driven tool_outputs/), proving the
generalization. Deterministic: drives the combination workflow (no network/LLM) via run_workflow.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()


def test_combination_run_writes_full_artifact_dir(tmp_path):
    """A real combination run (no network) writes report.md + data.json + a data-driven
    tool_outputs/ with one JSON per bundle part — the generic seam, on a non-epitope workflow."""
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.schemas.data_shapes import Bundle
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    handle = default_handle_store().put(
        Bundle(
            parts={
                "candidate_released": True,
                "candidate": {
                    "sequence": "ACDEFGHIKLMN",
                    "source_region": {"start": 10, "end": 21},
                },
                "approval": {"scope_query": "q", "protein": "ctx"},
            }
        )
    )
    out = asyncio.run(
        run_workflow(
            "epitope_combination_feasibility_assessment",
            {
                "candidate_assessment_handle": handle,
                "additional_epitopes": [
                    {
                        "label": "ep A",
                        "sequence": "QRSTVWYAA",
                        "start": 40,
                        "end": 48,
                        "source": "x",
                    }
                ],
            },
        )
    )
    # Withheld (no approval) is still a real completed run that produces artifacts.
    assert out["status"] in {"ok", "needs_input"}, out
    run_dir = tmp_path / "artifacts" / out["run_id"]
    assert (run_dir / "report.md").read_text().strip(), "report.md must be non-empty"
    data = json.loads((run_dir / "data.json").read_text())
    assert data.get("kind") == "bundle", data
    tools = run_dir / "tool_outputs"
    # Data-driven: one JSON per bundle part — keys from THIS (non-epitope) workflow, not hardcoded.
    written = {p.name for p in tools.glob("*.json")}
    assert "combination_released.json" in written, written
    assert "readiness.json" in written, written
    assert json.loads((tools / "combination_released.json").read_text()) is False
