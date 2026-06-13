"""Live (real Globus) test for the aggregate-served pdb/emdb harmonized_search path.

Proves PDB and EMDB are queryable through harmonized_search against the real
aggregate index e74bf12a, and that the publisher.name filter keeps the two
sources DISTINCT (a pdb query never returns emdb accessions, and vice versa).
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


needs_globus = pytest.mark.skipif(not _globus_reachable(), reason="Globus Search unreachable")


@needs_globus
def test_pdb_and_emdb_return_source_distinct_records():
    from apecx_integration.mcp_surface.tools.harmonized_search import harmonized_search

    out_pdb = asyncio.run(harmonized_search(term="spike glycoprotein", index="pdb"))
    out_emdb = asyncio.run(harmonized_search(term="spike glycoprotein", index="emdb"))

    assert out_pdb["status"] == "ok", out_pdb
    assert out_emdb["status"] == "ok", out_emdb

    pdb_md, emdb_md = out_pdb["markdown"], out_emdb["markdown"]
    # Each returns its own source's accessions (structure-rich term → both non-empty).
    assert "pdb:" in pdb_md, pdb_md[:500]
    assert "emdb:" in emdb_md, emdb_md[:500]
    # Source-distinctness: the publisher filter must not bleed sources across.
    assert "emdb:" not in pdb_md, "pdb query leaked EMDB accessions"
    assert "pdb:" not in emdb_md, "emdb query leaked PDB accessions"


@needs_globus
def test_pdb_tool_path_is_taxon_locked_no_cross_virus_leak():
    """E3-2.3 lockstep: the MCP tool taxon-locks a CHIKV term — West Nile excluded, hits non-empty."""
    from apecx_integration.mcp_surface.tools.harmonized_search import harmonized_search

    out = asyncio.run(harmonized_search(term="chikungunya envelope", index="pdb"))
    assert out["status"] == "ok", out
    md = out["markdown"]
    # CC-1: non-empty real result (at least one Globus PDB accession rendered).
    assert "[Globus pdb:" in md, md[:400]
    # Taxon precision: the free-text baseline leaks a West Nile structure; the tool must not.
    assert "west nile" not in md.lower(), "MCP tool leaked a West Nile structure"
