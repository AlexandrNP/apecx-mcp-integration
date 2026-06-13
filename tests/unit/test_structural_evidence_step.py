"""Unit tests for StructuralEvidenceStep — the PDB/EMDB structural leg.

Focus: the no-silent-failure contract. A no-hit and a Globus outage must BOTH
produce a loud, named ``structural_note`` — never a silent empty list.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.agents.globus_search.client import GlobusSearchUnavailableError
from apecx_integration.composition.steps import structural_evidence_step as mod
from apecx_integration.composition.steps.structural_evidence_step import StructuralEvidenceStep


def _stage(tmp_path: Path, **cfg) -> StructuralEvidenceStep:
    p = tmp_path / "structural.yml"
    body = "name: structural_test\n"
    for k, v in cfg.items():
        body += f"{k}: {v}\n"
    p.write_text(body)
    return StructuralEvidenceStep.from_config(str(p))


def _bundle(**over):
    b = {"query": "chikungunya structural polyprotein", "globus_results": []}
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "structural_test"


def test_no_hit_is_loud_not_silent(tmp_path, monkeypatch):
    """Zero structural records -> a NAMED structural_note, not a silent empty list."""
    step = _stage(tmp_path)
    monkeypatch.setattr(mod, "_DEFAULT_PUBLISHERS", {"pdb": "RCSB PDB"})

    def _empty(*a, **k):
        return []

    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _empty)
    out = asyncio.run(step.process(_bundle()))
    assert out["structural_records"] == []
    assert out["structural_note"] is not None
    assert "No PDB or EMDB structural records" in out["structural_note"]
    assert "chikungunya structural polyprotein" in out["structural_note"]


def test_globus_outage_is_loud_and_distinct_from_no_hit(tmp_path, monkeypatch):
    """A Globus failure sets a DIFFERENT loud note ('unavailable') — not a no-hit."""
    step = _stage(tmp_path)

    def _boom(*a, **k):
        raise GlobusSearchUnavailableError("network down")

    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _boom)
    out = asyncio.run(step.process(_bundle()))
    assert out["structural_records"] == []
    assert out["structural_note"] is not None
    assert "unavailable" in out["structural_note"].lower()
    assert "No PDB or EMDB structural records" not in out["structural_note"]


def test_hits_merge_into_globus_results_deduped_and_tagged(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(mod, "_DEFAULT_PUBLISHERS", {"pdb": "RCSB PDB", "emdb": "EMDB"})

    def _fake_search(query, *, max_results, filters, **k):
        pub = filters[0]["values"][0]
        if pub == "RCSB PDB":
            return [{"subject": "pdb:1I9G", "content": {"title": "X"}, "score": None}]
        return [{"subject": "emdb:EMD-1", "content": {"title": "Y"}, "score": None}]

    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", _fake_search)
    # Pre-seed globus_results with a duplicate pdb:1I9G to prove dedup-by-subject.
    out = asyncio.run(
        step.process(_bundle(globus_results=[{"subject": "pdb:1I9G", "content": {"title": "old"}}]))
    )
    assert out["structural_note"] is None
    subjects = [h["subject"] for h in out["globus_results"]]
    assert subjects.count("pdb:1I9G") == 1  # deduped
    assert "emdb:EMD-1" in subjects  # new one merged
    # records carry the source tag for deterministic rendering downstream
    tags = {r["subject"]: r.get("structural_source") for r in out["structural_records"]}
    assert tags["pdb:1I9G"] == "pdb" and tags["emdb:EMD-1"] == "emdb"


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    """process({structural_input: bundle}) behaves like process(bundle)."""
    step = _stage(tmp_path)
    monkeypatch.setattr("apecx_integration.agents.globus_search.client.search", lambda *a, **k: [])
    out = asyncio.run(step.process({"structural_input": _bundle()}))
    assert out["query"] == "chikungunya structural polyprotein"
    assert out["structural_note"] is not None


def test_missing_query_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"globus_results": []}))
