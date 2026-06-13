"""REAL-API integration tests for E3-3 residue-level functional validation.

Network-gated (auto-skip when EBI/UniProt/IEDB are unreachable, or when
``APECX_SKIP_LIVE_NET=1``). These hit the live SIFTS / UniProt / IEDB services on the
exact structure the pipeline selects for the CHIKV envelope case (PDB 2XFB → UniProt
Q1H8W5) and assert REAL, non-empty cross-checks (CC-1/CC-3).

The candidate residue 141 (chain A author numbering) is a real residue that bridges to
UniProt 950 — a real N-linked glycosylation feature. The coincidence surfaced here is
computed entirely from live API data (SIFTS offset + UniProt feature), not fabricated.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from apecx_integration.agents.functional.iedb_client import IedbClient
from apecx_integration.agents.functional.residue_annotation import gather_annotation_context
from apecx_integration.agents.functional.sifts_client import SiftsClient, chain_segments
from apecx_integration.agents.functional.uniprot_client import UniProtClient
from apecx_integration.composition.steps.functional_validation_step import (
    FunctionalValidationStep,
)


def _net_reachable() -> bool:
    if os.environ.get("APECX_SKIP_LIVE_NET") == "1":
        return False
    try:
        return (
            httpx.get(
                "https://www.ebi.ac.uk/pdbe/api/mappings/uniprot/2xfb", timeout=5.0
            ).status_code
            == 200
        )
    except Exception:
        return False


SKIP = "live network (EBI/UniProt/IEDB) not reachable — set APECX_SKIP_LIVE_NET=0 + connect"
pytestmark = pytest.mark.skipif(not _net_reachable(), reason=SKIP)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_FUNCTIONAL_CACHE", str(tmp_path / "fcache"))


def _step(tmp_path) -> FunctionalValidationStep:
    p = tmp_path / "functional.yml"
    p.write_text("name: functional_real\nfetch_residue_annotations: true\n")
    return FunctionalValidationStep.from_config(str(p))


def _bundle_2xfb(resis: list[int], chain: str = "A") -> dict:
    return {
        "query": "chikungunya envelope epitopes",
        "conserved_regions": [{"start": 0, "end": 4, "consensus": "AAAAA", "length": 5}],
        "violin_mappings": [{"synonym_id": "v1", "source": "VIOLIN_Vaccine"}],
        "bvbrc_genomes": [{"genome_id": "37124.10", "genome_name": "Chikungunya virus"}],
        "structural_reasoning": {
            "available": True,
            "pdb_id": "2XFB",
            "chain": chain,
            "exposed_residues": [{"resi": r} for r in resis],
        },
    }


# ----------------------------------------------------------------- SIFTS numbering lock


def test_sifts_2xfb_chain_a_resi1_is_unp810():
    async def run():
        async with SiftsClient() as c:
            return await c.get_mappings("2XFB")

    mappings = asyncio.run(run())
    assert mappings is not None and "Q1H8W5" in mappings
    segs_a = chain_segments(mappings, "A")
    assert segs_a, "chain A must have a UniProt mapping segment"
    # The load-bearing +809 fixture: author resi 1 → UniProt 810.
    seg = segs_a[0]
    assert seg["author_start"] == 1 and seg["unp_start"] == 810
    assert seg["offset"] == 809


# ----------------------------------------------------------------- UniProt features


def test_uniprot_q1h8w5_has_glycosylation():
    async def run():
        async with UniProtClient() as c:
            return await c.get_entry("Q1H8W5")

    entry = asyncio.run(run())
    assert entry is not None
    assert len(entry["features"]) >= 1
    glyco = [f for f in entry["features"] if f["type"] == "Glycosylation"]
    assert glyco, "Q1H8W5 must carry >=1 glycosylation feature"
    assert any(f["start"] == 950 for f in glyco), "the unp-950 N-glycosylation must be present"
    assert entry["sequence"] and entry["sequence"][949] == "N"


# ----------------------------------------------------------------- IEDB cs.{} syntax pin


def test_iedb_cs_syntax_returns_epitopes_for_known_antigen():
    """A known antigen (SARS-CoV-2 spike P0DTC2) returns >=1 linear epitope through the
    cs.{} containment filter. If IEDB changes the array-filter schema this errors (loud)."""

    async def run():
        async with IedbClient() as c:
            return await c.search_epitopes("P0DTC2")

    epitopes = asyncio.run(run())
    assert len(epitopes) >= 1
    assert all(e["linear_sequence"] for e in epitopes)


def test_iedb_named_absence_for_q1h8w5():
    """Q1H8W5 has no IEDB epitopes — a genuine, named absence (empty list, not an error)."""

    async def run():
        async with IedbClient() as c:
            return await c.search_epitopes("Q1H8W5")

    assert asyncio.run(run()) == []


# ----------------------------------------------------------------- gather context


def test_gather_context_2xfb_real():
    async def run():
        async with SiftsClient() as s, UniProtClient() as u, IedbClient() as i:
            return await gather_annotation_context("2XFB", "A", sifts=s, uniprot=u, iedb=i)

    ctx = asyncio.run(run())
    assert ctx["available"] is True
    assert "Q1H8W5" in ctx["accessions"]
    assert ctx["n_uniprot_features"] >= 1
    assert ctx["uniprot_release"]  # provenance present


# ----------------------------------------------------------------- FULL STEP, real data


def test_step_surfaces_real_glyco_coincidence(tmp_path):
    """CC-1 core: a real 2XFB candidate residue (141 → unp 950) surfaces a REAL coincidence,
    and the result is NEVER empty-coincidences-with-available=true."""
    step = _step(tmp_path)
    # 141 → unp 950 (real N-glycosylation); 50 → unp 859 is a genuine feature-free residue
    # (verified live: no UniProt feature covers unp 859) → explicit named absence.
    out = asyncio.run(step.process(_bundle_2xfb([141, 50])))
    fv = out["functional_validation"]

    assert fv["residue_level_annotation_available"] is True
    assert fv["annotation_source"] == "UniProt+SIFTS+IEDB"
    # The anti-empty core: available=true ⇒ (non-empty coincidences) OR (named per-residue list).
    assert fv["coincidences"] or fv["residue_findings"]
    assert not (
        fv["residue_level_annotation_available"]
        and not fv["coincidences"]
        and not fv["residue_findings"]
    )
    # The real glycosylation coincidence is surfaced.
    glyco = [c for c in fv["coincidences"] if c.get("type") == "Glycosylation"]
    assert glyco, f"expected a real glycosylation coincidence, got {fv['coincidences']}"
    assert glyco[0]["residue"] == 141 and glyco[0]["unp_pos"] == 950
    assert glyco[0]["accession"] == "Q1H8W5"
    # Per-residue named absence for the non-coinciding candidates.
    assert any(
        "no functional/immunological feature at residue 50" in f for f in fv["residue_findings"]
    )
    assert "Q1H8W5" in fv["assessment"] and "COINCIDE" in fv["assessment"]
    # Provenance + passthrough (CC-2): the bundle reaches review with a stage report.
    assert fv["uniprot_release"] and fv["query_date"]
    assert any(r["stage"] == "functional_validation" for r in out["stage_reports"])


def test_step_all_named_when_no_coincidence(tmp_path):
    """A real candidate set with NO overlapping feature → empty coincidences but a COMPLETE
    per-residue named-absence list (CC-1 degrade branch, never silent empty)."""
    step = _step(tmp_path)
    # 44/45/46 → unp 853/854/855 are all genuinely feature-free (verified live).
    out = asyncio.run(step.process(_bundle_2xfb([44, 45, 46])))
    fv = out["functional_validation"]
    assert fv["residue_level_annotation_available"] is True
    assert fv["coincidences"] == []
    assert len(fv["residue_findings"]) == 3
    assert all("no functional/immunological feature" in f for f in fv["residue_findings"])


# ----------------------------------------------------------------- degrade paths


def test_step_no_uniprot_xref_degrades_named(tmp_path):
    """A nonexistent / non-cross-referenced PDB → named note + bundle passes through (G127)."""
    step = _step(tmp_path)
    bundle = _bundle_2xfb([10, 20])
    bundle["structural_reasoning"]["pdb_id"] = "9ZZ9"  # no SIFTS UniProt xref
    out = asyncio.run(step.process(bundle))
    fv = out["functional_validation"]
    assert fv["residue_level_annotation_available"] is False
    assert fv["coincidences"] == []  # allowed ONLY because available is False
    assert fv["annotation_note"]  # the degrade is NAMED, not silent
    assert out["query"] == bundle["query"]  # passthrough


def test_gather_network_down_degrades_named():
    """A connection failure → available:false with a named note, never raises (CC-2)."""

    async def run():
        async with (
            SiftsClient(base_url="http://127.0.0.1:9/pdbe/api", retry_attempts=1) as s,
            UniProtClient() as u,
            IedbClient() as i,
        ):
            return await gather_annotation_context("2XFB", "A", sifts=s, uniprot=u, iedb=i)

    ctx = asyncio.run(run())
    assert ctx["available"] is False
    assert ctx["note"] and "2XFB" in ctx["note"]
