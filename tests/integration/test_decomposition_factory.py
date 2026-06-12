"""Assembled LocalDecomposer over the real catalog (EO capstone item 1).

Unit-level: the factory assembles, and the real catalog matcher picks the right workflow for a
conserved-sites query (network-free). Integration: solve() drives match → dispatch → a REAL
workflow run — which also proves the dispatcher's input_envelope_key wrapping (without it the
structured payload would deposit nothing and the run would fail, not return 'ok').
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
import requests

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/genome_feature/"
            f"?eq(taxon_id,{_CHIKV_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_deps = pytest.mark.skipif(
    shutil.which("mafft") is None or not _bvbrc_reachable(),
    reason="needs MAFFT installed AND BV-BRC reachable",
)


def test_factory_assembles():
    from apecx_integration.composition.decomposition.factory import assemble_local_decomposer
    from apecx_integration.composition.decomposition.local_decomposer import LocalDecomposer

    d = assemble_local_decomposer(settle_ms=2000)
    assert isinstance(d, LocalDecomposer)


def test_real_catalog_matcher_picks_conserved_sites():
    # Network-free: the keyword matcher over the live catalog descriptions resolves a
    # conserved-sites query to the right workflow (not rhea_muscle_alignment).
    from apecx_integration.composition.decomposition.local_decomposer import Task
    from apecx_integration.composition.decomposition.matchers import KeywordWorkflowMatcher
    from apecx_integration.mcp_surface.workflow_registry import load_catalog

    catalog = {e.tool_name: e.description for e in load_catalog().workflows}
    matcher = KeywordWorkflowMatcher(catalog)
    res = asyncio.run(matcher.match(Task("find conserved protein sites across virus strains")))
    assert res is not None
    assert res.workflow_name == "viral_conserved_sites"


@needs_deps
def test_solve_dispatches_real_conserved_sites_workflow():
    from apecx_integration.composition.decomposition.factory import assemble_local_decomposer
    from apecx_integration.composition.decomposition.local_decomposer import Task

    decomposer = assemble_local_decomposer(settle_ms=2000, timeout=600.0)
    res = asyncio.run(
        decomposer.solve(
            Task(
                "find conserved protein sites in chikungunya virus",
                payload={"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein"},
            )
        )
    )
    # The matcher routed to viral_conserved_sites; the dispatcher envelope-wrapped the structured
    # payload (input_envelope_key=fetch_in) so the real cascade ran to a conserved-sites result.
    assert res.status == "ok", res
    assert "Conserved sites" in res.markdown
