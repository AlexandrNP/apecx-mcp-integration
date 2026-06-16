"""END-TO-END against the path a WEAK model actually takes: the DIRECT first-class workflow
tool (registry-synthesized), NOT run_workflow().

This is the harness that would have caught the null regression. The earlier epitope null-fix +
clean-install verification drove ``run_workflow(name, params)`` — the GUARDED path — and was
green while the DIRECT tool (which routed through the old, unguarded ``_runner``) still returned
NULL on a G127 strand. These tests register the real catalog, grab the synthesized tool fns the
MCP client sees, call them by their typed signature, and assert the result is NEVER null — a
non-empty WorkflowResult envelope (markdown) OR a loud error/refusal — in BOTH loci.

Network-gated: the product workflows pull from the Globus/BV-BRC index. Skips cleanly when the
network/services are unavailable; the assertion is shape + non-null, never content (per the
synthesis-test directive). Real LLM is NOT required — desktop locus omits it (scaffold), and the
requires_llm gate turns a no-LLM agent run into a loud refusal, which is still non-null.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.mcp_surface.workflow_registry import load_catalog, register_workflows

pytestmark = pytest.mark.integration

# The catalog/promoted product WORKFLOWS exposed as direct tools via register_workflows — the
# ones that route through the unified guarded core (the null-prone path). NOTE: harmonized_search
# is a hand-written PRIMITIVE registered separately (not via register_workflows), so it is not in
# this set; it never had the registry-runner null. A weak model sends each its minimal params.
_DIRECT_TOOL_QUERIES = {
    "viral_epitope_analysis": {
        "query": "conserved epitopes on chikungunya virus E1",
        "protein": "E1",
    },
    # viral_conserved_sites takes a taxon + protein (NOT a free-text query) — its real signature.
    "viral_conserved_sites": {"taxon_id": 37124, "protein": "E1"},
    "rag_e2e_synthesis": {"query": "how do alphaviruses enter host cells?"},
}


class _CaptureServer:
    """Captures the synthesized tool fns register_workflows would put on the wire."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def _decorator(fn):
            self.tools[name] = fn
            return fn

        return _decorator


def _registered_direct_tools() -> dict[str, object]:
    srv = _CaptureServer()
    register_workflows(srv, load_catalog())
    return srv.tools


def _is_non_null(out: dict) -> bool:
    """A result is OK iff it carries SOMETHING actionable — markdown OR a loud error — and is
    never the G127 empty/None-markdown trap the unification fixed."""
    if not isinstance(out, dict):
        return False
    md = out.get("markdown")
    err = out.get("error")
    return bool((md and str(md).strip()) or (err and str(err).strip()))


@pytest.fixture
def restore_locus():
    prior = get_active_locus()
    yield
    set_active_locus(prior)


def test_all_product_direct_tools_are_registered():
    tools = _registered_direct_tools()
    for name in _DIRECT_TOOL_QUERIES:
        assert name in tools, f"product workflow {name!r} is not a first-class direct tool"


@pytest.mark.parametrize("locus", [ExecutionLocus.DESKTOP, ExecutionLocus.AGENT])
@pytest.mark.parametrize("name", sorted(_DIRECT_TOOL_QUERIES))
def test_direct_tool_never_returns_null(name, locus, restore_locus):
    """The weak-model path: call the DIRECT tool by its typed signature; assert NON-NULL in
    both loci. Skips on a network/service error (which is itself a loud, non-null envelope —
    we only skip to keep CI green offline, not because null is acceptable)."""
    if os.environ.get("APECX_SKIP_LIVE_LLM") == "1" and locus is ExecutionLocus.AGENT:
        pytest.skip("APECX_SKIP_LIVE_LLM=1 — agent-locus internal synthesis opted out")
    set_active_locus(locus)
    tool = _registered_direct_tools()[name]
    out = asyncio.run(tool(**_DIRECT_TOOL_QUERIES[name]))
    assert _is_non_null(out), f"{name} ({locus.value}) returned a NULL/empty result: {out!r}"
    # When the workflow actually RAN to completion (status 'ok'), it must carry the run_id
    # stamped by the unified core — proof it went through the guarded path, not a bypass. A
    # needs_input control-return or a loud refusal is non-null but does NOT run, so no run_id.
    if out.get("status") == "ok":
        assert out.get("run_id"), f"{name} success envelope missing run_id (unguarded path?)"
