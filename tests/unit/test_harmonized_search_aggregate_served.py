"""Unit tests for the aggregate-served (pdb/emdb) branch of harmonized_search.

These exercise the tool-level structural path that bypasses the taxonomy
resolve→execute workflow. Globus is monkeypatched — no network.
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.agents.globus_search.client import GlobusSearchUnavailableError
from apecx_integration.mcp_surface.tools import harmonized_search as mod
from apecx_integration.mcp_surface.tools.harmonized_search import harmonized_search

_SEARCH = "apecx_integration.agents.globus_search.client.search"


def test_pdb_emdb_are_valid_indices():
    assert {"pdb", "emdb"} <= mod._VALID_INDICES
    # ...and they are the aggregate-served category, not taxonomy indices.
    assert {"pdb", "emdb"}.isdisjoint(mod._TAXONOMY_INDICES)


def test_pdb_uses_rcsb_publisher_filter_and_lists_records(monkeypatch):
    captured = {}

    def _fake(term, *, max_results, filters):
        captured["term"] = term
        captured["filters"] = filters
        return [{"subject": "pdb:1I9G", "content": {"title": "Crystal X"}}]

    monkeypatch.setattr(_SEARCH, _fake)
    out = asyncio.run(harmonized_search(term="spike glycoprotein", index="pdb"))
    assert out["status"] == "ok"
    assert "[Globus pdb:1I9G]" in out["markdown"] and "Crystal X" in out["markdown"]
    # publisher.name is the source discriminator within the shared aggregate index.
    assert captured["filters"][0]["field_name"] == "publisher.name"
    assert captured["filters"][0]["values"] == ["RCSB PDB"]
    assert captured["term"] == "spike glycoprotein"


def test_emdb_uses_emdb_publisher_filter(monkeypatch):
    captured = {}

    def _fake(term, *, max_results, filters):
        captured["filters"] = filters
        return []

    monkeypatch.setattr(_SEARCH, _fake)
    asyncio.run(harmonized_search(term="cryo-EM", index="emdb"))
    assert captured["filters"][0]["values"] == ["Electron Microscopy Data Bank"]


def test_no_hit_is_loud(monkeypatch):
    monkeypatch.setattr(_SEARCH, lambda term, **k: [])
    out = asyncio.run(harmonized_search(term="obscurevirus", index="pdb"))
    assert out["status"] == "ok"  # a no-hit is a valid result, not an error
    assert "No records found" in out["markdown"]  # ...but it is NAMED, never silent


def test_globus_outage_is_loud_error(monkeypatch):
    def _boom(term, **k):
        raise GlobusSearchUnavailableError("network down")

    monkeypatch.setattr(_SEARCH, _boom)
    out = asyncio.run(harmonized_search(term="x", index="pdb"))
    assert out["status"] == "error"
    assert out["error"] and "network down" in out["error"]


def test_invalid_index_still_raises():
    with pytest.raises(ValueError):
        asyncio.run(harmonized_search(term="x", index="not_a_real_index"))


def test_empty_term_still_raises():
    with pytest.raises(ValueError):
        asyncio.run(harmonized_search(term="  ", index="pdb"))
