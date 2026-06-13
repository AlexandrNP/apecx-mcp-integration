"""Integration test for the structural-reasoning stage (E2-P) — REAL containerized PyMOL.

Gated on Docker being available AND the version-pinned ``apecx-pymol`` image being
present AND RCSB being reachable (the host fetches the immutable structure). It runs
the actual ``StructuralReasoningStep.process()`` against a REAL chikungunya envelope
structure (3N40, E1/E2 glycoprotein) with REAL conserved-region motifs, and asserts
that real solvent-exposed / buried residue NUMBERS come back from real PyMOL SASA —
plus byte-level determinism across two runs.

Build the image first (one-time, ~5 min):

    docker build -t apecx-pymol:3.1.0 -f docker/pymol/Dockerfile docker/pymol
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import urllib.request

import pytest

pytestmark = pytest.mark.integration

_IMAGE = "apecx-pymol:3.1.0"
# 3N40 = CHIKV E1/E2 glycoprotein. Chain F is E1; this 14-mer is a real contiguous
# segment of that chain, so it maps deterministically (a live MSA produces such
# consensus motifs; here we anchor on a known-present real subsequence so the test
# is hermetic w.r.t. the upstream sequence stage while keeping the STRUCTURE + SASA
# fully real).
_PDB_RECORD = {"subject": "pdb:3N40", "structural_source": "pdb"}
_REAL_REGION = {"start": 33, "end": 46, "length": 14, "consensus": "MVLEMELLSVTLEP"}
_ABSENT_REGION = {"start": 200, "end": 211, "length": 12, "consensus": "WWWWWWWWWWWW"}


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", _IMAGE], capture_output=True, timeout=20
        )
        return out.returncode == 0
    except Exception:
        return False


def _require_rcsb(pdb_id: str = "3N40") -> None:
    """Runtime (not import-time) network check, so a transient blip skips THIS test
    instead of stalling collection of the whole file for the urlopen timeout."""
    try:
        url = f"https://files.rcsb.org/download/{pdb_id}.cif"
        with urllib.request.urlopen(url, timeout=15) as r:
            if r.status != 200:
                pytest.skip("RCSB returned non-200")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"RCSB not reachable: {exc}")


# Gate on the (cheap, deterministic) image-present check at import; the network
# dependency is checked at runtime via _require_rcsb().
_GATE = pytest.mark.skipif(not _image_present(), reason=f"requires the {_IMAGE} container image")


def _step():
    import tempfile
    from pathlib import Path

    from apecx_integration.composition.steps.structural_reasoning_step import (
        StructuralReasoningStep,
    )

    cfg = Path(tempfile.mkdtemp(prefix="apecx_pymol_cfg_")) / "reasoning_int.yml"
    cfg.write_text("name: reasoning_int\nrsa_threshold: 0.25\nmin_map_identity: 0.7\n")
    return StructuralReasoningStep.from_config(str(cfg))


def _bundle():
    return {
        "query": "chikungunya E1 glycoprotein conserved epitopes",
        "conserved_regions": [dict(_REAL_REGION), dict(_ABSENT_REGION)],
        "structural_records": [dict(_PDB_RECORD)],
    }


@_GATE
def test_real_pymol_sasa_exposed_residues():
    _require_rcsb()
    step = _step()
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]

    assert sr["available"] is True, sr.get("note")
    assert sr["pdb_id"] == "3N40"
    assert sr["pymol_version"] == "3.1.0"
    assert sr["sasa_settings"] == {"dot_solvent": 1, "dot_density": 3}
    assert sr["chain"] == "F"

    # The real motif maps; the all-W motif does not (loud note).
    assert sr["n_mapped_regions"] == 1
    assert sr["n_mapped_residues"] == 14
    assert any("WWWW" in n for n in sr.get("notes", []))

    # Real exposed / buried split, with concrete residue numbers.
    assert sr["n_exposed"] + sr["n_buried"] == sr["n_mapped_residues"]
    assert sr["n_exposed"] >= 1
    exposed_resis = [e["resi"] for e in sr["exposed_residues"]]
    assert all(isinstance(r, int) for r in exposed_resis)
    # Every exposed residue clears the RSA cutoff with a real SASA value.
    for e in sr["exposed_residues"]:
        assert e["state"] == "exposed"
        assert e["rsa"] >= 0.25
        assert e["sasa"] > 0.0

    # Contact map computed over the mapped residues.
    assert isinstance(sr["contacts"], list)

    # The stage report surfaces the real exposed residue numbers for the synthesis trace.
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "solvent-exposed" in rep["markdown"]
    assert str(exposed_resis[0]) in rep["markdown"]


@_GATE
def test_real_pymol_sasa_is_deterministic():
    _require_rcsb()
    step = _step()
    a = asyncio.run(step.process(_bundle()))["structural_reasoning"]
    b = asyncio.run(step.process(_bundle()))["structural_reasoning"]
    assert [e["resi"] for e in a["exposed_residues"]] == [e["resi"] for e in b["exposed_residues"]]
    assert [e["sasa"] for e in a["exposed_residues"]] == [e["sasa"] for e in b["exposed_residues"]]
    assert [e["resi"] for e in a["buried_residues"]] == [e["resi"] for e in b["buried_residues"]]


# --------------------------------------------------------------- E3-1: biological assembly SASA
# 2XFB = CHIKV mature E1/E2 glycoprotein. Its biological assembly 1 is the T=4
# icosahedral envelope shell (60 MODELs / copies). Residues 298-312 of chain A (E2)
# include an inter-protomer LATTICE-CONTACT cluster (302/304/305/307) that reads
# solvent-EXPOSED on the deposited asymmetric unit but is BURIED once the assembly's
# symmetry-mate copies are present — the exact wrong-accessibility error E3-1 fixes.
_2XFB_RECORD = {"subject": "pdb:2XFB", "structural_source": "pdb"}
_2XFB_REGION = {"start": 298, "end": 312, "length": 15, "consensus": "DMSCEVPACTHSSDF"}


@_GATE
def test_assembly_sasa_differs_from_au_on_2xfb():
    """Load-bearing proof: SASA over the BIOLOGICAL ASSEMBLY changes >=1 residue's
    exposed/buried verdict vs the asymmetric unit (the fix is real, not cosmetic), and
    the assembly classification is non-empty (CC-1) + byte-stable across two runs (CC-4)."""
    _require_rcsb("2XFB")
    from apecx_integration.composition.steps import _pymol_sasa as sasa
    from apecx_integration.composition.steps.structural_reasoning_step import _fetch_structure

    step = _step()
    regions = [dict(_2XFB_REGION)]
    asm_path, asm_kind = _fetch_structure("2XFB")
    au_path, au_kind = _fetch_structure("2XFB", prefer_assembly=False)
    assert asm_kind == "assembly_1"
    assert au_kind == "asymmetric_unit"

    au = asyncio.run(step._run_pymol_on_file("2XFB", au_path, au_kind, regions))
    asm = asyncio.run(step._run_pymol_on_file("2XFB", asm_path, asm_kind, regions))
    asm2 = asyncio.run(step._run_pymol_on_file("2XFB", asm_path, asm_kind, regions))

    assert au["ok"] and asm["ok"], (au.get("note"), asm.get("note"))
    # CC-1: the assembly run classifies >=1 real residue (non-empty), assembly-tagged.
    assert asm["structure_kind"] == "assembly_1"
    assert asm["assembly_id"] == 1
    assert asm["n_assembly_copies"] == 60
    assert asm["neighbor_cutoff"] == 10.0
    assert asm["chain"] == "A"
    assert asm["n_mapped_residues"] == 15
    assert asm["n_exposed"] + asm["n_buried"] == asm["n_mapped_residues"]
    assert asm["n_exposed"] >= 1

    # The load-bearing assertion: at least one residue's verdict CHANGES AU -> assembly,
    # and it is the known lattice-contact cluster reading exposed (AU) -> buried (assembly).
    flips = sasa.assembly_exposure_flips(
        au["exposed_residues"] + au["buried_residues"],
        asm["exposed_residues"] + asm["buried_residues"],
    )
    assert len(flips) >= 1, f"assembly changed NO verdict (cosmetic): au={au} asm={asm}"
    flipped = {f["resi"] for f in flips}
    assert flipped & {302, 304, 305, 307}, flipped
    assert all(f["au_state"] == "exposed" and f["assembly_state"] == "buried" for f in flips), flips
    # The assembly buries residues the AU calls exposed -> fewer exposed in the assembly.
    assert asm["n_exposed"] < au["n_exposed"]

    # CC-4: byte-stable assembly SASA across two runs (same residues + same SASA values).
    assert [e["resi"] for e in asm["exposed_residues"]] == [
        e["resi"] for e in asm2["exposed_residues"]
    ]
    assert [e["sasa"] for e in asm["exposed_residues"]] == [
        e["sasa"] for e in asm2["exposed_residues"]
    ]
    assert [e["sasa"] for e in asm["buried_residues"]] == [
        e["sasa"] for e in asm2["buried_residues"]
    ]


@_GATE
def test_no_assembly_degrades_to_au_named_caveat(monkeypatch):
    """E3-1.3 / CC-2: a real PDB whose biological assembly is NOT available in legacy
    PDB format (.pdb1.gz 404) falls back to the AU and emits the NAMED caveat. The
    404 -> AU fetch decision is REAL (7K00, a 70S ribosome whose assembly exceeds legacy
    PDB limits); the full-step SASA runs over a real (small, cached) AU so the degrade
    path returns a NON-EMPTY classification (CC-1) carrying the caveat."""
    _require_rcsb("2XFB")
    from apecx_integration.composition.steps import structural_reasoning_step as srmod
    from apecx_integration.composition.steps.structural_reasoning_step import _fetch_structure

    # REAL: RCSB 404 on the legacy assembly -> the fetch returns the AU, kind-tagged.
    _, kind_7k00 = _fetch_structure("7K00")
    assert kind_7k00 == "asymmetric_unit"

    # Full step over a real AU: steer the fetch to the cached 2XFB AU (kept small + fast)
    # tagged as asymmetric_unit, so the AU-classification + caveat wiring runs on real SASA.
    au_path, _ = _fetch_structure("2XFB", prefer_assembly=False)
    monkeypatch.setattr(srmod, "_fetch_structure", lambda pdb_id, **k: (au_path, "asymmetric_unit"))
    step = _step()
    out = asyncio.run(
        step.process(
            {
                "query": "chikungunya E2 glycoprotein conserved epitopes",
                "protein": "envelope glycoprotein E2",
                "conserved_regions": [dict(_2XFB_REGION)],
                "structural_records": [dict(_2XFB_RECORD)],
            }
        )
    )
    sr = out["structural_reasoning"]
    assert sr["available"] is True, sr.get("note")
    assert sr["structure_kind"] == "asymmetric_unit"
    assert sr["assembly_id"] is None
    assert sr["n_mapped_residues"] >= 1  # CC-1: non-empty classification on the AU
    assert "asymmetric unit" in sr["assembly_caveat"]
    assert "2XFB" in sr["assembly_caveat"]
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "asymmetric unit" in rep["markdown"]
    assert rep["data"]["structure_kind"] == "asymmetric_unit"


# --------------------------------------------------- E3-13: multi-structure corroboration
# Three REAL CHIKV E1-glycoprotein structures: 3N40 (mature E1/E2 spike, E1 = chain F),
# 6NK7 (E1/E2, E1 = chain A), 2XFB (icosahedral envelope, E1 motif on chain A). The E1
# conserved motif MVLEMELLSVTLEP (alignment cols 33-46) maps onto the E1 chain of ALL
# three (CHIKV E1 shares author numbering across these deposits). Per-residue assembly
# SASA over each gives a REAL corroboration gradient: motif position resi 34 reads
# solvent-EXPOSED in 3N40 + 6NK7 + 2XFB (3/3), resi 39 in 3N40 + 2XFB (2/3, majority),
# resi 37 in 6NK7 only (1/3, minority) — the load-bearing proof that cross-structure
# corroboration is real, not cosmetic. Cross-structure correspondence is by the SHARED
# (region, motif_index) coordinate, NOT the PDB residue number.
_MULTI_CORPUS = [
    {"subject": "pdb:3N40", "structural_source": "pdb"},
    {"subject": "pdb:6NK7", "structural_source": "pdb"},
    {"subject": "pdb:2XFB", "structural_source": "pdb"},
]
_E1_REGION = {"start": 33, "end": 46, "length": 14, "consensus": "MVLEMELLSVTLEP"}


def _multi_step():
    import tempfile
    from pathlib import Path

    from apecx_integration.composition.steps.structural_reasoning_step import (
        StructuralReasoningStep,
    )

    cfg = Path(tempfile.mkdtemp(prefix="apecx_pymol_cfg_")) / "reasoning_multi.yml"
    cfg.write_text(
        "name: reasoning_multi\nrsa_threshold: 0.25\nmin_map_identity: 0.7\nmax_structures: 3\n"
    )
    return StructuralReasoningStep.from_config(str(cfg))


def _multi_bundle():
    return {
        "query": "chikungunya E1 glycoprotein conserved epitopes",
        "protein": "envelope glycoprotein E1 E2",
        "conserved_regions": [dict(_E1_REGION)],
        "structural_records": [dict(r) for r in _MULTI_CORPUS],
    }


@_GATE
def test_multi_structure_corroboration_on_real_chikv(capsys):
    """E3-13 / CC-1: analyse the top-3 ranked CHIKV E1 structures, corroborate candidate
    epitope residues across them, and prove >=1 residue is exposed in >1 structure (the
    whole point). Byte-stable across two runs (CC-4)."""
    for pid in ("3N40", "6NK7", "2XFB"):
        _require_rcsb(pid)
    step = _multi_step()
    out = asyncio.run(step.process(_multi_bundle()))
    sr = out["structural_reasoning"]

    assert sr["available"] is True, sr.get("note")
    # >=2 structures actually analysed (non-empty per-structure list).
    assert sr["n_analyzed_structures"] >= 2
    analyzed_ok = [s for s in sr["analyzed_structures"] if s["available"]]
    assert len(analyzed_ok) >= 2

    # PRIMARY supplies the back-compat single-structure shape functional validation reads.
    assert sr["pdb_id"] == "3N40"
    assert sr["chain"] == "F"
    primary_exposed = [e["resi"] for e in sr["exposed_residues"]]
    assert primary_exposed  # non-empty candidate set (CC-1)

    # Aggregated candidate set is non-empty AND >=1 residue is corroborated across >1 structure.
    assert sr["corroboration"]
    multi = [c for c in sr["corroboration"] if c["exposed_in_k"] > 1]
    assert multi, f"no residue exposed in >1 structure: {sr['corroboration']}"
    # Headline corroborated set anchored to the primary, non-empty, with K/N counts.
    corr = [r for r in sr["corroborated_residues"] if r["corroborated"]]
    assert corr
    assert any(r["exposed_in_k"] > 1 for r in corr)

    # CC-4: byte-stable aggregated result across two runs.
    out2 = asyncio.run(step.process(_multi_bundle()))
    sr2 = out2["structural_reasoning"]
    assert sr["corroboration"] == sr2["corroboration"]
    assert sr["corroborated_residues"] == sr2["corroborated_residues"]
    assert [s["pdb_id"] for s in sr["analyzed_structures"]] == [
        s["pdb_id"] for s in sr2["analyzed_structures"]
    ]

    # PASTE the real multi-structure + corroboration output (load-bearing proof).
    with capsys.disabled():
        print("\n=== E3-13 real CHIKV multi-structure corroboration ===")
        print("analyzed structures:")
        for s in sr["analyzed_structures"]:
            print(
                f"  {s['pdb_id']:6s} avail={s['available']} chain={s.get('chain')} "
                f"kind={s.get('structure_kind')} exposed={s.get('n_exposed')} "
                f"mapped={s.get('n_mapped_residues')}"
            )
        print(f"primary={sr['pdb_id']} chain={sr['chain']} exposed_residues={primary_exposed}")
        print("per-position corroboration (shared region/motif coordinate):")
        for c in sr["corroboration"]:
            print(
                f"  region {c['region_start']}-{c['region_end']} motif_idx={c['motif_index']} "
                f"aa={c['consensus_aa']}: exposed in {c['exposed_in_k']}/{c['analyzed_n']} "
                f"{c['exposed_pdb_ids']} | resi_by_pdb={c['resi_by_pdb']} "
                f"corroborated={c['corroborated']}"
            )
        print("headline corroborated candidate-epitope residues (anchored to primary):")
        for r in sr["corroborated_residues"]:
            print(
                f"  resi {r['resi']} ({r['consensus_aa']}): exposed in "
                f"{r['exposed_in_k']}/{r['analyzed_n']} {r['exposed_pdb_ids']} "
                f"corroborated={r['corroborated']}"
            )
        print(f"n_corroborated={sr['n_corroborated']}")
