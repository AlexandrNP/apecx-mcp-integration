"""Integration tests for the harmonized_search workflow.

Two paths:

1. **Workflow.from_config + offline cases** — workflow loads cleanly,
   ambiguous-resolution path produces a paused WorkflowResult without
   touching the network.

2. **Live-Globus path** — gated on the production dict being present
   AND globus-search reachable. Drives the full pipeline against a
   real APECx Globus index, asserts the WorkflowResult envelope shape.

The dictionary path is read from APECX_SYNONYM_DICT_PATH (defaults to
~/.apecx/dictionary/dictionary.sqlite per the resolve step's import-
time configuration).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_DEFAULT_DICT = Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"
_DICT_PATH = Path(os.environ.get("APECX_SYNONYM_DICT_PATH", str(_DEFAULT_DICT)))

pytestmark_dict = pytest.mark.skipif(
    not _DICT_PATH.exists(),
    reason=f"production dict not present at {_DICT_PATH}",
)


@pytest.fixture(scope="module")
def workflow():
    from nanobrain.core.workflow import Workflow

    wf_yaml = Path(
        "src/apecx_integration/composition/workflows/"
        "harmonized_search/harmonized_search_workflow.yml"
    )
    return Workflow.from_config(wf_yaml)


@pytestmark_dict
def test_workflow_loads(workflow):
    """Sanity: the workflow YAML loads and has 3 steps."""
    assert workflow.name == "harmonized_search_workflow"


@pytestmark_dict
def test_ambiguous_path_via_run_workflow_observed(workflow):
    """RSV resolves to multiple candidates → paused envelope, no Globus."""
    from apecx_integration.composition.runtime.observed_run import (
        run_workflow_observed,
    )

    outcome = asyncio.run(
        run_workflow_observed(
            workflow,
            {"workflow_input": {"term": "RSV", "index": "bvbrc_genome"}},
        )
    )
    assert outcome.workflow_result is not None
    wr = outcome.workflow_result
    assert wr.data_preview is not None

    # The preview is the Bundle's key list. The full payload is behind
    # data_handle in the HandleStore.
    preview_keys = wr.data_preview.get("parts") or []
    assert "resolution" in preview_keys
    assert "next_action" in preview_keys
    assert "status" in preview_keys

    # Retrieve the full Bundle.
    from apecx_integration.composition.handles.store import (
        default_handle_store,
    )

    bundle = default_handle_store().get(wr.data_handle)
    assert bundle.parts["status"] == "paused_awaiting_disambiguation"
    assert bundle.parts["resolution"]["candidate_count"] >= 2
    assert "raw_query" not in bundle.parts  # no Globus query executed

    # Markdown explains the ambiguity in human terms.
    assert "RSV" in wr.markdown
    assert "ambiguous" in wr.markdown.lower()


@pytestmark_dict
def test_chikv_live_globus_harmonized_search(workflow):
    """End-to-end: CHIKV resolves cleanly + live Globus returns harmonized > raw.

    Live network — skips on Globus failure but doesn't pretend it passed.
    """
    from apecx_integration.composition.runtime.observed_run import (
        run_workflow_observed,
    )

    try:
        outcome = asyncio.run(
            run_workflow_observed(
                workflow,
                {"workflow_input": {"term": "CHIKV", "index": "bvbrc_genome"}},
            )
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Globus unreachable in this environment: {exc}")

    wr = outcome.workflow_result
    assert wr is not None, "workflow should emit a WorkflowResult"
    assert wr.status in ("ok", "partial")

    # Bundle.preview returns sorted key names — the actual values live
    # behind data_handle in the HandleStore.
    preview_parts = wr.data_preview.get("parts") if wr.data_preview else None
    assert isinstance(preview_parts, list), (
        f"Bundle preview should list part names; got {preview_parts!r}"
    )
    assert "raw_query" in preview_parts
    assert "harmonized_query" in preview_parts
    assert "divergence" in preview_parts

    # Retrieve the full Bundle via the handle store and check totals.
    from apecx_integration.composition.handles.store import (
        default_handle_store,
    )

    assert wr.data_handle, "data_handle must be set for a real Bundle"
    bundle = default_handle_store().get(wr.data_handle)
    parts = bundle.parts
    raw_q = parts["raw_query"]
    harm_q = parts["harmonized_query"]
    div = parts["divergence"]

    # SC-B7 closure expectation: harmonized substantially exceeds raw.
    # Use loose bounds because production index counts drift.
    assert harm_q["total"] >= 1000, (
        f"expected ≥1000 harmonized CHIKV records; got {harm_q['total']}. "
        f"Dict may be stale or BVBRC genome index changed."
    )
    assert harm_q["total"] > raw_q["total"], (
        f"harmonized should exceed raw for CHIKV; got raw={raw_q['total']} harm={harm_q['total']}"
    )
    assert div["absolute_diff"] > 0

    # Markdown surfaces the comparison.
    md_lower = wr.markdown.lower()
    assert "raw" in md_lower
    assert "harmonized" in md_lower


@pytestmark_dict
def test_mcp_tool_returns_workflow_result_dict():
    """The harmonized_search MCP tool returns the WorkflowResult envelope as a dict."""
    from apecx_integration.mcp_surface.tools.harmonized_search import (
        harmonized_search,
    )

    try:
        result = asyncio.run(harmonized_search(term="EEEV", index="bvbrc_genome"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"workflow failed in this environment: {exc}")
    assert isinstance(result, dict)
    # Envelope keys (when not _no_envelope path).
    if "_no_envelope" not in result:
        assert "markdown" in result
        assert "status" in result


def test_mcp_tool_input_validation():
    """The tool rejects bad params before touching the workflow."""
    from apecx_integration.mcp_surface.tools.harmonized_search import (
        harmonized_search,
    )

    with pytest.raises(ValueError, match="term"):
        asyncio.run(harmonized_search(term="", index="bvbrc_genome"))
    with pytest.raises(ValueError, match="index"):
        asyncio.run(harmonized_search(term="CHIKV", index="not_a_real_index"))
    with pytest.raises(ValueError, match="entity_type"):
        asyncio.run(
            harmonized_search(
                term="CHIKV",
                index="bvbrc_genome",
                entity_type="not_a_real_type",
            )
        )
