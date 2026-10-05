"""Live end-to-end test for the literature_rag MCP tool (builder path).

Drives the WHOLE built workflow via ``Workflow.run`` in DESKTOP locus on real
data: real PubMed harvest → real taxon stamping (real dictionary) → real organism
resolve → ontology filter → desktop evidence hand-off. This is the mocks-parity
partner for tests/unit/test_literature_rag_mcp_tool.py (the harvest + desktop
inversion there are mocked).

Gated: skips when the dictionary is absent (stamp/resolve would be meaningless) or
the PubMed harvest yields nothing (network).
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"


def _dict_present() -> bool:
    try:
        from apecx_integration.synonym_dictionary.loader import default_dictionary_path

        return default_dictionary_path().exists()
    except Exception:
        return False


needs_dict = pytest.mark.skipif(not _dict_present(), reason="synonym dictionary absent")


@needs_dict
def test_literature_rag_desktop_e2e_live():
    """A CHIKV literature question runs end-to-end in desktop locus: the host gets
    the ontology-filtered evidence (status=host_synthesizes) with on-organism PMIDs,
    no apecx LLM required."""
    from apecx_integration.composition.runtime.execution_locus import (
        ExecutionLocus,
        get_active_locus,
        set_active_locus,
    )
    from apecx_integration.composition.workflows.literature_rag.builder import (
        build_literature_rag_workflow,
    )

    prior = get_active_locus()
    set_active_locus(ExecutionLocus.DESKTOP)
    try:
        w = build_literature_rag_workflow()
        result = asyncio.run(
            w.run(
                {
                    "literature_rag_input": {
                        "organism": "Chikungunya virus",
                        "question": "What is known about CHIKV E1 epitopes?",
                        "max_papers": 15,
                    }
                },
                timeout=300.0,
                settle_ms=2000,
                await_cascade=True,
            )
        )
    finally:
        set_active_locus(prior)

    # G127 honesty: decide success from the OUTPUT VALUE, not the run's status field.
    # Workflow.run returns the output data unit at the TOP level (keyed by its name),
    # alongside a 'status' field — NOT nested under an 'outputs' key.
    assert isinstance(result, dict), f"unexpected run result: {result!r}"
    envelope = result.get("workflow_output")
    assert isinstance(envelope, dict), f"no workflow_output envelope: {result}"

    # Desktop locus: host synthesizes. Either we found on-organism papers
    # (host_synthesizes + cited PMIDs) or PubMed/filter honestly yielded none.
    if envelope.get("status") == "no_evidence":
        pytest.skip("PubMed harvest / ontology filter yielded no CHIKV papers for the probe")
    assert envelope["status"] == "host_synthesizes", envelope
    assert envelope["citations"], "expected at least one cited PMID"
    assert "[PMID:" in envelope["answer"]


@needs_dict
def test_literature_rag_registered_in_server_tool_list():
    """The literature_rag tool is present on the live MCP server surface."""
    from mcp.server.fastmcp import FastMCP

    from apecx_integration.mcp_surface.workflow_registry import load_catalog, register_workflows

    server = FastMCP("test-apecx")
    register_workflows(server, load_catalog())
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "literature_rag" in names, sorted(names)
