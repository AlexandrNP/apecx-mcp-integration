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


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "version"], capture_output=True, timeout=20).returncode == 0
        )
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_find_and_establish_builds_pymol_image_through_the_seam():
    """Phase-3a unification: the docker tool is established THROUGH the find_and_establish seam.
    ``find_and_establish_tool('pymol:pymol_sasa')`` auto-builds apecx-pymol:3.1.0 if absent (vs the
    old path where the adapter self-provisioned around the seam). Idempotent: a fast no-op image
    probe when already present, a real build (~5 min) when absent."""
    from nanobrain.library.tools.tool_discovery import find_and_establish_tool

    from apecx_integration.composition.steps.pymol_sasa_tool import PyMOLToolBackendAdapter

    PyMOLToolBackendAdapter.register()  # so the seam resolves the "pymol" backend
    utds = asyncio.run(find_and_establish_tool("pymol:pymol_sasa"))
    assert utds[0].descriptor_id == "pymol:pymol_sasa@0.0.0"
    assert _image_present() is True  # the seam established (built-or-confirmed) the image


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


# --------------------------------------------------- R1: mmCIF large-assembly fallback
# 6N1D = a 70S ribosome X-ray crystal with TWO ribosomes in the deposited file. Its
# biological assembly has >62 chains, so RCSB serves NO legacy ``.pdb1`` — assembly 1
# (ONE ribosome) exists only as ``6N1D-assembly1.cif``. The current code would 404 on
# .pdb1 and silently use the deposited AU (.cif = BOTH ribosomes), over-burying the
# inter-ribosome crystal contacts. Chain AS04 (small-subunit protein S4) carries a
# crystal-contact cluster (resi 16/18) that reads BURIED in the 2-ribosome AU but
# correctly EXPOSED in the 1-ribosome biological assembly — the wrong-accessibility R1
# fixes. (Honest scope: RCSB serves legacy .pdb1 for symmetry-EXPANDED viral capsids,
# even million-atom ones, so the mmCIF-only path is reached for large multi-chain /
# multi-assembly deposits like this, not for the icosahedral-capsid case the flag imagined.)
_6N1D_RECORD = {"subject": "pdb:6N1D", "structural_source": "pdb"}
_6N1D_CHAIN = "AS04"
_6N1D_REGION = {"start": 10, "end": 23, "length": 14, "consensus": "RLCRREGVKLYLKG"}


@_GATE
def test_mmcif_assembly_fetch_path_and_sasa_differs_from_au():
    """R1 / CC-1 / CC-4: a real PDB whose biological assembly is mmCIF-only (.pdb1 404)
    now (a) fetches as ``mmcif_assembly`` not a silent AU, (b) computes a NON-EMPTY real
    SASA classification over the assembly, (c) flips >=1 residue's verdict vs the AU
    (proving the assembly is really used), and (d) is byte-stable across two runs."""
    _require_rcsb("6N1D")
    from apecx_integration.composition.steps import _pymol_sasa as sasa
    from apecx_integration.composition.steps.structural_reasoning_step import _fetch_structure

    step = _step()
    regions = [dict(_6N1D_REGION)]
    # R1 fetch decision (REAL): .pdb1 404 -> mmCIF assembly (NOT a silent AU).
    asm_path, asm_kind = _fetch_structure("6N1D")
    au_path, au_kind = _fetch_structure("6N1D", prefer_assembly=False)
    assert asm_kind == "mmcif_assembly", asm_kind
    assert au_kind == "asymmetric_unit"

    asm = asyncio.run(
        step._run_pymol_on_file("6N1D", asm_path, asm_kind, regions, requested_chain=_6N1D_CHAIN)
    )
    au = asyncio.run(
        step._run_pymol_on_file("6N1D", au_path, au_kind, regions, requested_chain=_6N1D_CHAIN)
    )
    asm2 = asyncio.run(
        step._run_pymol_on_file("6N1D", asm_path, asm_kind, regions, requested_chain=_6N1D_CHAIN)
    )
    assert au["ok"] and asm["ok"], (au.get("note"), asm.get("note"))

    # CC-1: the mmCIF assembly run classifies the real chain non-empty, assembly-tagged.
    assert asm["structure_kind"] == "mmcif_assembly"
    assert asm["assembly_id"] == 1
    assert asm["chain"] == _6N1D_CHAIN
    assert asm["n_mapped_residues"] == 14
    assert asm["n_exposed"] + asm["n_buried"] == asm["n_mapped_residues"]
    assert asm["n_exposed"] >= 1

    # Load-bearing: >=1 residue's verdict CHANGES AU -> assembly (the assembly is really
    # used, not silently the AU). The 2-ribosome deposited AU spuriously BURIES crystal-
    # contact residues that the 1-ribosome biological assembly correctly EXPOSES.
    flips = sasa.assembly_exposure_flips(
        au["exposed_residues"] + au["buried_residues"],
        asm["exposed_residues"] + asm["buried_residues"],
    )
    assert len(flips) >= 1, f"mmCIF assembly changed NO verdict (cosmetic): au={au} asm={asm}"
    assert {f["resi"] for f in flips} & {16, 18}, flips
    assert all(f["au_state"] == "buried" and f["assembly_state"] == "exposed" for f in flips), flips
    assert asm["n_exposed"] > au["n_exposed"]  # assembly un-buries the crystal contacts

    # CC-4: byte-stable assembly SASA across two runs (same residues + same SASA values).
    assert [e["resi"] for e in asm["exposed_residues"]] == [
        e["resi"] for e in asm2["exposed_residues"]
    ]
    assert [e["sasa"] for e in asm["exposed_residues"]] == [
        e["sasa"] for e in asm2["exposed_residues"]
    ]


@_GATE
def test_no_assembly_degrades_to_au_named_caveat(monkeypatch):
    """E3-1.3 / CC-2: a structure with NO biological assembly in EITHER format falls back to
    the AU and emits the NAMED caveat with a NON-EMPTY classification (CC-1). The genuine
    .pdb1-404 -> mmCIF-404 -> AU fetch decision is covered deterministically by the unit
    tests (``test_fetch_falls_back_to_au_only_when_no_assembly_anywhere`` /
    ``test_fetch_mmcif_assembly_returns_none_on_404``) — a released RCSB entry with NO
    assembly in either format is vanishingly rare (R1 finding: RCSB serves the legacy .pdb1
    for every symmetry-expanded assembly and the mmCIF assembly for the large ones), so here
    we steer the fetch to a real cached AU and assert the AU classification + caveat wiring on
    real SASA."""
    _require_rcsb("2XFB")
    from apecx_integration.composition.steps import structural_reasoning_step as srmod
    from apecx_integration.composition.steps.structural_reasoning_step import _fetch_structure

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


# --------------------------------------------- R3: chain-pinning for multi-structure corroboration
# 6JO8 = "CHIKV envelope glycoprotein bound to human MXRA8". Its biological assembly carries
# protein chains A (entity 'Togavirin' — the auto-picked FIRST chain), B (CHIKV E1) and O
# (the MXRA8 receptor). The default-chain-only behaviour auto-picks A, so the E1 conserved
# region does NOT map and 6JO8 does NOT corroborate. R3 chain-pinning maps the SAME E1 region
# onto chain B (E1), so 6JO8 corroborates — raising coverage. (The conserved protein IS
# genuinely present in 6JO8, just under a different chain letter; chain-pinning finds it.)
_6JO8_RECORD = {"subject": "pdb:6JO8", "structural_source": "pdb"}
_R3_E1_REGION = {"start": 33, "end": 46, "length": 14, "consensus": "MVLEMELLSVTLEP"}


@_GATE
def test_chain_pinning_selects_e1_chain_not_autopick_on_6jo8(capsys):
    """R3 before/after on ONE real structure: the auto-picked first chain (A, 'Togavirin')
    MISSES the E1 conserved region; chain-pinning analyses chain B (E1) where it maps."""
    _require_rcsb("6JO8")
    from apecx_integration.composition.steps.structural_reasoning_step import _fetch_structure

    step = _step()
    regions = [dict(_R3_E1_REGION)]
    path, kind = _fetch_structure("6JO8")

    # BEFORE (default-chain-only): pin the auto-picked first protein chain A -> E1 absent.
    before = asyncio.run(step._run_pymol_on_file("6JO8", path, kind, regions, requested_chain="A"))
    # AFTER (R3 chain-pinning): no pin -> the job selects the best motif-mapping chain.
    after = asyncio.run(step._run_pymol_on_file("6JO8", path, kind, regions))

    assert before["ok"] and after["ok"], (before.get("note"), after.get("note"))
    # BEFORE: chain A is analysed but the E1 region maps onto NOTHING (loud note).
    assert before["chain"] == "A"
    assert before["n_mapped_regions"] == 0
    assert before["n_mapped_residues"] == 0
    # AFTER: chain-pinning analyses the E1 chain (B) where the region maps at full identity.
    assert after["chain"] == "B", after["chain"]
    assert after["chain_selected_by"] == "best_motif_identity"
    assert after["n_candidate_chains"] >= 2
    assert after["n_mapped_regions"] == 1
    assert after["n_mapped_residues"] == 14
    assert after["n_exposed"] >= 1  # CC-1: a non-empty candidate-epitope set on the E1 chain

    with capsys.disabled():
        print("\n=== R3 chain-pinning before/after on 6JO8 (CHIKV E1 + MXRA8 complex) ===")
        print(
            f"  BEFORE (auto-pick first chain): chain={before['chain']} "
            f"mapped_regions={before['n_mapped_regions']} mapped_residues={before['n_mapped_residues']} "
            f"-> E1 conserved region MISSED, 6JO8 would NOT corroborate"
        )
        print(
            f"  AFTER  (best-chain pinning):    chain={after['chain']} "
            f"(of {after['n_candidate_chains']} candidate chains) "
            f"mapped_regions={after['n_mapped_regions']} mapped_residues={after['n_mapped_residues']} "
            f"exposed={after['n_exposed']} -> E1 region mapped on the E1 chain, 6JO8 corroborates"
        )


_R3_CORPUS = [
    {"subject": "pdb:3N40", "structural_source": "pdb"},  # E1 = chain F (auto-pick OK)
    {"subject": "pdb:6NK7", "structural_source": "pdb"},  # E1 = chain A (auto-pick OK)
    {"subject": "pdb:6JO8", "structural_source": "pdb"},  # E1 = chain B (auto-pick A MISSES)
]


@_GATE
def test_chain_pinning_raises_corroboration_on_real_chikv(capsys):
    """R3 end-to-end: with chain-pinning, the E1 conserved region maps onto the E1 chain of
    ALL THREE structures (3N40/F, 6NK7/A, 6JO8/B) — including 6JO8, whose auto-picked chain A
    would have missed it — so 6JO8 now contributes to cross-structure corroboration."""
    for pid in ("3N40", "6NK7", "6JO8"):
        _require_rcsb(pid)
    import tempfile
    from pathlib import Path

    from apecx_integration.composition.steps.structural_reasoning_step import (
        StructuralReasoningStep,
    )

    cfg = Path(tempfile.mkdtemp(prefix="apecx_pymol_cfg_")) / "reasoning_r3.yml"
    cfg.write_text(
        "name: reasoning_r3\nrsa_threshold: 0.25\nmin_map_identity: 0.7\nmax_structures: 3\n"
    )
    step = StructuralReasoningStep.from_config(str(cfg))
    bundle = {
        "query": "chikungunya E1 glycoprotein conserved epitopes",
        "protein": "envelope glycoprotein E1 E2",
        "conserved_regions": [dict(_R3_E1_REGION)],
        "structural_records": [dict(r) for r in _R3_CORPUS],
    }
    out = asyncio.run(step.process(bundle))
    sr = out["structural_reasoning"]
    assert sr["available"] is True, sr.get("note")

    by_pdb = {s["pdb_id"]: s for s in sr["analyzed_structures"]}
    assert "6JO8" in by_pdb, by_pdb
    j = by_pdb["6JO8"]
    # Chain-pinning analysed 6JO8 on the E1 chain B (NOT the auto-picked Togavirin chain A).
    assert j["available"] is True, j
    assert j["chain"] == "B", j
    assert j["n_mapped_residues"] == 14

    # 6JO8 genuinely contributes: >=1 corroboration position is exposed in 6JO8 AND in >=1
    # other structure (so chain-pinning RAISED corroboration coverage, not a self-count).
    contributing = [
        c for c in sr["corroboration"] if "6JO8" in c["exposed_pdb_ids"] and c["exposed_in_k"] >= 2
    ]
    assert contributing, sr["corroboration"]

    with capsys.disabled():
        print("\n=== R3 chain-pinning raises corroboration (real CHIKV 3N40/6NK7/6JO8) ===")
        print("analyzed structures (chain chosen by motif-identity pinning):")
        for s in sr["analyzed_structures"]:
            print(
                f"  {s['pdb_id']:6s} chain={s.get('chain')} mapped={s.get('n_mapped_residues')} "
                f"exposed={s.get('n_exposed')}"
            )
        print("corroboration positions 6JO8 contributes to (exposed in 6JO8 + >=1 other):")
        for c in contributing:
            print(
                f"  motif_idx={c['motif_index']} aa={c['consensus_aa']}: exposed in "
                f"{c['exposed_in_k']}/{c['analyzed_n']} {c['exposed_pdb_ids']} "
                f"resi_by_pdb={c['resi_by_pdb']}"
            )
