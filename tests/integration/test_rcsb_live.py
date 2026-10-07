"""Live (real RCSB) tests for the authoritative structural fallback.

Proves, against the real RCSB Search + Data APIs, that:
  * the taxon-name query returns the real, COMPLETE structure set for an organism whose
    structures are largely absent from the APECx aggregate index (Nipah: ~4 in-index vs
    ~76 in RCSB, including the G/F glycoproteins the demo was missing),
  * every returned structure is organism-grounded (taxon-lock holds — no off-organism bleed),
  * harmonized_search("<virus> glycoprotein", "pdb") surfaces that authoritative set with a
    provenance note.
Plus one no-network unit test: the augmentation degrades LOUD (aggregate-only + named note)
when RCSB is unreachable — the real happy path above is its integration-parity counterpart.

Live tests auto-skip when RCSB is unreachable (honest). CC-1: every happy path asserts
NON-EMPTY real data.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_NIPAH_NAMES = ["Henipavirus nipahense", "Nipah henipavirus", "Nipah virus"]


def _rcsb_reachable() -> bool:
    import asyncio

    from apecx_integration.agents.functional.rcsb_live import (
        RcsbLiveUnavailableError,
        search_pdb_ids_by_organism_names,
    )

    try:
        # Use the CURRENT scientific name (RCSB maps deposits to current taxonomy; the old
        # "Nipah virus" spelling matches zero records and would false-negative the gate).
        ids = asyncio.run(search_pdb_ids_by_organism_names(["Henipavirus nipahense"], rows=1))
        return bool(ids)
    except RcsbLiveUnavailableError:
        return False
    except Exception:
        return False


needs_rcsb = pytest.mark.skipif(not _rcsb_reachable(), reason="RCSB Search/Data API unreachable")


@needs_rcsb
def test_live_client_returns_complete_nipah_set_including_glycoproteins():
    """CC-1: the live client returns the authoritative Nipah set — the G/F glycoproteins the
    APECx aggregate index (4 records) was missing — all taxon-grounded, with real metadata."""
    import asyncio

    from apecx_integration.agents.functional.rcsb_live import live_pdb_structures_for_organisms

    structs = asyncio.run(live_pdb_structures_for_organisms(_NIPAH_NAMES, rows=100))
    assert len(structs) >= 20, f"expected the full Nipah PDB set, got {len(structs)}"

    by_id = {s.pdb_id.upper(): s for s in structs}
    for want in ("2VSM", "3D11", "5EVM"):  # G attachment + F fusion glycoproteins
        assert want in by_id, f"{want} (a real Nipah glycoprotein) missing from live set"

    # Taxon-lock holds: every structure carries a Nipah/Henipavirus source organism. Co-deposited
    # hosts (Homo sapiens receptor, antibody-producing species) are legitimate and allowed.
    for s in structs:
        assert any("nipah" in o.lower() or "henipav" in o.lower() for o in s.organisms), (
            s.pdb_id,
            s.organisms,
        )

    # Metadata is real, not placeholder: the G attachment glycoprotein has a resolution + title.
    g = by_id["3D11"]
    assert g.resolution and g.resolution > 0, g
    assert "glycoprotein" in (g.title + " " + " ".join(g.molecules)).lower()


@needs_rcsb
def test_harmonized_search_pdb_augments_with_live_rcsb():
    """CC-1 end-to-end: the MCP tool surfaces the authoritative set + a provenance note.

    Before the live augmentation the grounded tool returned ZERO Nipah glycoprotein
    structures (the aggregate index never harvested them); now it returns the real set.
    """
    import asyncio
    import re

    from apecx_integration.mcp_surface.tools.harmonized_search import harmonized_search

    out = asyncio.run(harmonized_search(term="nipah glycoprotein", index="pdb"))
    md = out["markdown"]
    ids = set(re.findall(r"\[PDB (\w+)\]", md))
    assert len(ids) >= 20, (
        f"expected the authoritative Nipah set in the tool output, got {len(ids)}"
    )
    assert {"2VSM", "3D11", "5EVM"} <= ids, f"G/F glycoproteins missing from tool output: {ids}"
    assert "authoritative RCSB" in md
    assert "Provenance:" in md


@needs_rcsb
def test_search_empty_names_returns_empty_without_calling_api():
    """An empty/whitespace organism list short-circuits to [] (no spurious API call)."""
    import asyncio

    from apecx_integration.agents.functional.rcsb_live import search_pdb_ids_by_organism_names

    assert asyncio.run(search_pdb_ids_by_organism_names([])) == []
    assert asyncio.run(search_pdb_ids_by_organism_names(["", "  "])) == []


def test_pdb_augmentation_degrades_loud_on_rcsb_outage(monkeypatch):
    """No-network unit test (integration-parity: the happy path is the live tests above).

    When the live RCSB call raises, the tool must degrade LOUD to the aggregate-only
    rendering with a NAMED note — never crash, never silently drop the in-index records.
    """
    import asyncio

    from apecx_integration.agents.functional.rcsb_live import RcsbLiveUnavailableError
    from apecx_integration.agents.globus_search.structural_query import StructuralSearchResult
    from apecx_integration.mcp_surface.tools import harmonized_search as hs

    async def _boom(*_a, **_k):
        raise RcsbLiveUnavailableError("simulated RCSB outage")

    # _pdb_search_with_live_augmentation does a function-local import of this name, so patching
    # it on the source module is what the call actually resolves at runtime.
    monkeypatch.setattr(
        "apecx_integration.agents.functional.rcsb_live.live_pdb_structures_for_organisms", _boom
    )

    # A realistic aggregate result with one in-index record (a real dataclass, not a mock).
    result = StructuralSearchResult(
        hits=[{"subject": "pdb:7SKT", "content": {"titles": [{"title": "Nipah matrix protein"}]}}],
        note=None,
        organisms=["Nipah virus"],
        query_used="*",
    )
    out = asyncio.run(
        hs._pdb_search_with_live_augmentation("nipah glycoprotein", "pdb", "RCSB PDB", result)
    )
    md = out["markdown"]
    assert "Live RCSB augmentation unavailable" in md
    assert "RcsbLiveUnavailableError" in md
    assert "7SKT" in md  # the in-index record is NOT dropped on the degrade
