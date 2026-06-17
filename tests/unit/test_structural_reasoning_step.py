"""Unit tests for the structural-reasoning stage (E2-P).

Two layers, both PyMOL/Docker-free:

1. ``_pymol_sasa`` — the PURE scientific logic shared with the containerized PyMOL
   job: conserved-motif → structure-residue mapping (ungapped sliding window),
   relative-SASA exposed/buried classification, PDB-id extraction. The SASA
   *numbers* the integration run produces are real PyMOL output; here we verify the
   arithmetic the job calls on those numbers, with realistic SASA fixtures (a dict
   ``resn → Å²`` — a data shape, not a mock of an interface).
2. ``StructuralReasoningStep`` — the DEGRADE-LOUD contract (G127): no candidate
   structure / no conserved regions / Docker-or-image unavailable must each produce
   a NAMED note in both the bundle and the stage report and pass the bundle through,
   never raise — so ``merge → reasoning → review`` always reaches synthesis.

The end-to-end real-PDB → real-SASA → real exposed/buried path is covered by the
Docker-gated integration test ``tests/integration/test_structural_reasoning_pymol.py``.
"""

from __future__ import annotations

import asyncio
import gzip
import urllib.error
from pathlib import Path

import pytest

from apecx_integration.composition.steps import _pymol_sasa as sasa
from apecx_integration.composition.steps import structural_reasoning_step as mod
from apecx_integration.composition.steps.structural_reasoning_step import StructuralReasoningStep

# --------------------------------------------------------------------- _pymol_sasa


def test_extract_pdb_id_variants():
    assert sasa.extract_pdb_id({"subject": "pdb:1I9G"}) == "1I9G"
    assert sasa.extract_pdb_id({"subject": "PDB:3n40", "structural_source": "pdb"}) == "3N40"
    assert sasa.extract_pdb_id({"subject": "6NK7"}) == "6NK7"
    # EMDB density maps are not loadable as coordinates.
    assert sasa.extract_pdb_id({"subject": "emdb:EMD-34119", "structural_source": "emdb"}) is None
    assert sasa.extract_pdb_id({"subject": "not-a-pdb-id"}) is None
    assert sasa.extract_pdb_id({}) is None


def test_select_candidate_prefers_first_loadable():
    records = [
        {"subject": "emdb:EMD-1", "structural_source": "emdb"},
        {"subject": "pdb:3N40", "structural_source": "pdb"},
        {"subject": "pdb:6NK7", "structural_source": "pdb"},
    ]
    assert sasa.select_candidate_pdb_id(records) == "3N40"
    assert sasa.select_candidate_pdb_id([]) is None


def test_map_motif_exact():
    chain_seq = "AAAMVLEMELLSVTLEPGGG"
    resis = list(range(10, 10 + len(chain_seq)))
    m = sasa.map_motif_to_chain("MVLEMELLSVTLEP", chain_seq, resis, min_identity=0.7)
    assert m is not None
    assert m["offset"] == 3
    assert m["identity"] == 1.0
    assert [r["resi"] for r in m["residues"]] == list(range(13, 27))
    assert all(r["match"] for r in m["residues"])


def test_map_motif_tolerates_strain_mismatch():
    # Structure strain differs at one position; ungapped identity 13/14 >= 0.7 → maps.
    chain_seq = "AAAMVLAMELLSVTLEPGGG"  # E->A at index 6 of motif
    resis = list(range(1, 1 + len(chain_seq)))
    m = sasa.map_motif_to_chain("MVLEMELLSVTLEP", chain_seq, resis, min_identity=0.7)
    assert m is not None
    assert m["identity"] == round(13 / 14, 4)
    assert sum(1 for r in m["residues"] if not r["match"]) == 1


def test_map_motif_below_threshold_returns_none():
    chain_seq = "QWERTYQWERTYQWERTY"
    resis = list(range(len(chain_seq)))
    assert sasa.map_motif_to_chain("MVLEMELLSVTLEP", chain_seq, resis, min_identity=0.7) is None


def test_map_motif_longer_than_chain_or_empty():
    assert sasa.map_motif_to_chain("MVLEMELLSVTLEP", "AAA", [1, 2, 3]) is None
    assert sasa.map_motif_to_chain("", "AAAA", [1, 2, 3, 4]) is None


def test_map_motif_lowest_offset_tiebreak():
    # Motif present twice; deterministic choice is the lowest offset.
    chain_seq = "MVMVMV"
    resis = [1, 2, 3, 4, 5, 6]
    m = sasa.map_motif_to_chain("MV", chain_seq, resis, min_identity=1.0)
    assert m["offset"] == 0
    assert [r["resi"] for r in m["residues"]] == [1, 2]


# -------------------------------------------------------- R3: chain-pinning (best-chain)

_MOTIF_REGION = {"start": 33, "end": 46, "consensus": "MVLEMELLSVTLEP"}


def _chain_seq(seq: str, start: int = 1):
    """(resis, seq) for a chain — a data shape, not a mock (the real chain sequences come
    from PyMOL ``cmd.get_model`` in the integration run)."""
    return list(range(start, start + len(seq))), seq


def test_map_regions_on_chain_maps_dedups_and_notes_absent():
    resis, seq = _chain_seq("AAAMVLEMELLSVTLEPGGG", start=10)  # motif at offset 3 -> resi 13..26
    regions = [dict(_MOTIF_REGION), {"start": 200, "end": 211, "consensus": "WWWWWWWWWWWW"}]
    mapped, mapped_resis, notes = sasa.map_regions_on_chain(
        resis, seq, regions, min_identity=0.7, chain="F", pdb_id="3N40"
    )
    assert len(mapped) == 1
    assert mapped_resis == list(range(13, 27))
    assert mapped[0]["map_identity"] == 1.0
    # the absent (all-W) region is reported LOUD, naming the chain + pdb.
    assert any("WWWW" in n and "chain F of 3N40" in n for n in notes)


def test_select_best_chain_picks_mapping_chain_over_nonmapping():
    """R3 / the real 6JO8 case: the E1 motif maps onto chain B (E1) but NOT chain A (a
    different protein — 6JO8's auto-picked 'Togavirin') → best-chain selects B, not first."""
    a_resis, a_seq = _chain_seq("QWERTYQWERTYQWERTYQW")  # no motif
    b_resis, b_seq = _chain_seq("AAAMVLEMELLSVTLEPGGG", start=100)  # motif -> resi 103..116
    winner = sasa.select_best_chain(
        [("A", a_resis, a_seq), ("B", b_resis, b_seq)],
        [dict(_MOTIF_REGION)],
        min_identity=0.7,
        pdb_id="6JO8",
    )
    assert winner["chain"] == "B"
    assert len(winner["mapped_regions"]) == 1
    assert winner["mapped_resis"] == list(range(103, 117))
    assert winner["notes"] == []  # B maps -> no unmapped-region note on the winner


def test_select_best_chain_tiebreak_prefers_first_candidate():
    """Back-compat: when the motif maps equally well on >1 chain, the FIRST candidate wins
    (deterministic) — preserves the previous 'first protein chain' pick (e.g. 2XFB chain A)."""
    a_resis, a_seq = _chain_seq("AAAMVLEMELLSVTLEPGGG", start=1)
    b_resis, b_seq = _chain_seq("AAAMVLEMELLSVTLEPGGG", start=200)
    winner = sasa.select_best_chain(
        [("A", a_resis, a_seq), ("B", b_resis, b_seq)], [dict(_MOTIF_REGION)], min_identity=0.7
    )
    assert winner["chain"] == "A"


def test_select_best_chain_higher_identity_beats_earlier_chain():
    """A later chain that maps at HIGHER identity beats an earlier lower-identity chain."""
    a_resis, a_seq = _chain_seq("AAAMVLAMELLSVTLEPGGG", start=1)  # one mismatch -> 13/14
    b_resis, b_seq = _chain_seq("AAAMVLEMELLSVTLEPGGG", start=50)  # exact
    winner = sasa.select_best_chain(
        [("A", a_resis, a_seq), ("B", b_resis, b_seq)], [dict(_MOTIF_REGION)], min_identity=0.7
    )
    assert winner["chain"] == "B"
    assert winner["mapped_regions"][0]["map_identity"] == 1.0


def test_select_best_chain_no_false_match_when_absent_everywhere():
    """When the conserved protein is genuinely NOT in the structure (no chain maps), the
    FIRST chain is returned with EMPTY mapped_regions + a loud note — never a false match."""
    a_resis, a_seq = _chain_seq("QWERTYQWERTYQWERTYQW")
    b_resis, b_seq = _chain_seq("KLKLKLKLKLKLKLKLKLKL")
    winner = sasa.select_best_chain(
        [("A", a_resis, a_seq), ("B", b_resis, b_seq)],
        [dict(_MOTIF_REGION)],
        min_identity=0.7,
        pdb_id="9XXX",
    )
    assert winner["chain"] == "A"  # falls back to the first concrete chain
    assert winner["mapped_regions"] == []
    assert winner["mapped_resis"] == []
    assert any("did not map" in n for n in winner["notes"])


def test_select_best_chain_empty_returns_none():
    assert sasa.select_best_chain([], [dict(_MOTIF_REGION)]) is None


def test_classify_sasa_exposed_buried_unknown():
    # GLU max-ASA 223 Å²; 81.6/223 = 0.366 -> exposed (>= 0.25).
    exposed = sasa.classify_sasa("GLU", 81.623, rsa_threshold=0.25)
    assert exposed["state"] == "exposed"
    assert exposed["rsa"] == round(81.623 / 223.0, 4)
    # LEU max-ASA 201 Å²; 5.0/201 = 0.025 -> buried.
    buried = sasa.classify_sasa("LEU", 5.0, rsa_threshold=0.25)
    assert buried["state"] == "buried"
    # Non-standard residue -> unknown, never silently buried.
    unknown = sasa.classify_sasa("XYZ", 50.0)
    assert unknown["state"] == "unknown"
    assert unknown["rsa"] is None


def test_classify_sasa_deterministic():
    """Same residue + same SASA -> identical classification twice (byte-stable dict)."""
    a = sasa.classify_sasa("GLU", 81.623, rsa_threshold=0.25)
    b = sasa.classify_sasa("GLU", 81.623, rsa_threshold=0.25)
    assert a == b


# ------------------------------------------------- assembly_exposure_flips (E3-1, AU vs ASM)


def test_assembly_exposure_flips_detects_real_changes():
    """A residue that reads EXPOSED on the asymmetric unit but BURIED in the biological
    assembly is reported as a flip — the load-bearing proof the assembly fix is real."""
    au = [
        {"resi": 10, "state": "exposed"},
        {"resi": 11, "state": "buried"},
        {"resi": 12, "state": "exposed"},
    ]
    asm = [
        {"resi": 10, "state": "buried"},  # buried by a symmetry-mate in the assembly
        {"resi": 11, "state": "buried"},  # unchanged
        {"resi": 12, "state": "exposed"},  # unchanged
    ]
    assert sasa.assembly_exposure_flips(au, asm) == [
        {"resi": 10, "au_state": "exposed", "assembly_state": "buried"}
    ]


def test_assembly_exposure_flips_skips_unknown_and_missing():
    """``unknown`` residues and residues present on only one side are never counted as
    flips (no false positive from a non-standard residue or a mapping gap)."""
    au = [{"resi": 1, "state": "unknown"}, {"resi": 2, "state": "exposed"}]
    asm = [{"resi": 1, "state": "buried"}, {"resi": 3, "state": "buried"}]
    assert sasa.assembly_exposure_flips(au, asm) == []


def test_assembly_exposure_flips_deterministic_and_sorted():
    au = [{"resi": 30, "state": "exposed"}, {"resi": 5, "state": "exposed"}]
    asm = [{"resi": 30, "state": "buried"}, {"resi": 5, "state": "buried"}]
    a = sasa.assembly_exposure_flips(au, asm)
    b = sasa.assembly_exposure_flips(au, asm)
    assert a == b
    assert [f["resi"] for f in a] == [5, 30]


# --------------------------------------------------- E3-13: cross-structure corroboration


def _struct_result(pdb_id: str, *, exposed: list, buried: list, regions: list) -> dict:
    """A realistic per-structure PyMOL-job result (data shape, not an interface mock).

    ``regions`` is the per-structure ``mapped_regions`` (carrying the SHARED conserved-region
    coordinate start/end/consensus + this structure's author residue numbers)."""
    return {
        "ok": True,
        "pdb_id": pdb_id,
        "exposed_residues": [{"resi": r, "state": "exposed"} for r in exposed],
        "buried_residues": [{"resi": r, "state": "buried"} for r in buried],
        "mapped_regions": regions,
    }


def test_aggregate_corroboration_counts_k_of_n():
    """A conserved-region motif maps onto 3 structures with DIFFERENT author numbering;
    corroboration is keyed by the shared (region, motif_index) coordinate, not the resi.
    Motif index 0 is exposed in 3/3, index 1 in 2/3 (majority), index 2 in 1/3 (minority)."""
    region = {"start": 10, "end": 12, "consensus": "ABC"}
    # Structure S1: author resis 100,101,102; all three exposed.
    s1 = _struct_result(
        "1AAA",
        exposed=[100, 101, 102],
        buried=[],
        regions=[{**region, "residues": [100, 101, 102]}],
    )
    # Structure S2: author resis 200,201,202 (DIFFERENT numbering); idx0+idx1 exposed, idx2 buried.
    s2 = _struct_result(
        "2BBB",
        exposed=[200, 201],
        buried=[202],
        regions=[{**region, "residues": [200, 201, 202]}],
    )
    # Structure S3: author resis 300,301,302; only idx0 exposed.
    s3 = _struct_result(
        "3CCC",
        exposed=[300],
        buried=[301, 302],
        regions=[{**region, "residues": [300, 301, 302]}],
    )
    corr = sasa.aggregate_corroboration([s1, s2, s3], threshold=0.5)
    by_idx = {c["motif_index"]: c for c in corr}

    assert by_idx[0]["exposed_in_k"] == 3 and by_idx[0]["analyzed_n"] == 3
    assert by_idx[0]["corroborated"] is True
    assert by_idx[0]["exposed_pdb_ids"] == ["1AAA", "2BBB", "3CCC"]
    assert by_idx[0]["consensus_aa"] == "A"

    # 2/3 = majority -> corroborated.
    assert by_idx[1]["exposed_in_k"] == 2
    assert by_idx[1]["corroborated"] is True
    assert by_idx[1]["exposed_pdb_ids"] == ["1AAA", "2BBB"]

    # 1/3 = minority -> NOT corroborated.
    assert by_idx[2]["exposed_in_k"] == 1
    assert by_idx[2]["corroborated"] is False
    assert by_idx[2]["exposed_pdb_ids"] == ["1AAA"]

    # Shared-coordinate correspondence: the per-pdb author resis are preserved per position.
    assert by_idx[1]["resi_by_pdb"] == {"1AAA": 101, "2BBB": 201, "3CCC": 301}


def test_aggregate_corroboration_skips_failed_structures():
    """A per-structure failure (ok=False) is excluded; the rest still aggregate, and N
    reflects only the SUCCESSFUL structures."""
    region = {"start": 5, "end": 7, "consensus": "MN"}
    ok1 = _struct_result(
        "1AAA", exposed=[10, 11], buried=[], regions=[{**region, "residues": [10, 11]}]
    )
    ok2 = _struct_result(
        "2BBB", exposed=[20], buried=[21], regions=[{**region, "residues": [20, 21]}]
    )
    failed = {"ok": False, "pdb_id": "3CCC", "note": "container error"}
    corr = sasa.aggregate_corroboration([ok1, failed, ok2], threshold=0.5)
    assert all(c["analyzed_n"] == 2 for c in corr)  # only the 2 successes
    by_idx = {c["motif_index"]: c for c in corr}
    assert by_idx[0]["exposed_in_k"] == 2 and by_idx[0]["corroborated"] is True
    # 1/2 = 0.5 >= threshold 0.5 -> corroborated True (at least half).
    assert by_idx[1]["exposed_in_k"] == 1 and by_idx[1]["corroborated"] is True


def test_aggregate_corroboration_deterministic_and_single_structure():
    """N=1: every exposed position is exposed in 1/1 -> corroborated. Byte-stable across runs."""
    region = {"start": 0, "end": 2, "consensus": "PQ"}
    s = _struct_result("1AAA", exposed=[5, 6], buried=[], regions=[{**region, "residues": [5, 6]}])
    a = sasa.aggregate_corroboration([s], threshold=0.5)
    b = sasa.aggregate_corroboration([s], threshold=0.5)
    assert a == b
    assert all(c["analyzed_n"] == 1 and c["corroborated"] for c in a)


def test_corroborated_residue_list_anchors_to_primary():
    """The headline corroboration is anchored to the PRIMARY structure's author resis, with
    each residue's K/N count from the shared-coordinate aggregation."""
    region = {"start": 10, "end": 12, "consensus": "ABC"}
    primary = _struct_result(
        "1AAA",
        exposed=[100, 101, 102],
        buried=[],
        regions=[{**region, "residues": [100, 101, 102]}],
    )
    s2 = _struct_result(
        "2BBB",
        exposed=[200, 201],
        buried=[202],
        regions=[{**region, "residues": [200, 201, 202]}],
    )
    s3 = _struct_result(
        "3CCC",
        exposed=[300],
        buried=[301, 302],
        regions=[{**region, "residues": [300, 301, 302]}],
    )
    corr = sasa.aggregate_corroboration([primary, s2, s3], threshold=0.5)
    res = sasa.corroborated_residue_list(primary, corr)
    by_resi = {r["resi"]: r for r in res}
    assert set(by_resi) == {100, 101, 102}  # the primary's exposed residues
    assert by_resi[100]["exposed_in_k"] == 3 and by_resi[100]["corroborated"] is True
    assert by_resi[101]["exposed_in_k"] == 2 and by_resi[101]["corroborated"] is True
    assert by_resi[102]["exposed_in_k"] == 1 and by_resi[102]["corroborated"] is False
    # Sorted by resi (deterministic).
    assert [r["resi"] for r in res] == [100, 101, 102]


# --------------------------------------------------------------- StructuralReasoningStep


def _step(tmp_path: Path, **cfg) -> StructuralReasoningStep:
    p = tmp_path / "reasoning.yml"
    body = "name: reasoning_test\n"
    for k, v in cfg.items():
        body += f"{k}: {v}\n"
    p.write_text(body)
    return StructuralReasoningStep.from_config(str(p))


def _bundle(**over):
    b = {
        "query": "chikungunya E1 glycoprotein epitopes",
        "conserved_regions": [{"start": 0, "end": 4, "consensus": "MVLEM", "length": 5}],
        "structural_records": [{"subject": "pdb:3N40", "structural_source": "pdb"}],
    }
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    step = _step(tmp_path, rsa_threshold=0.3, min_map_identity=0.8)
    assert step.name == "reasoning_test"
    assert step._rsa_threshold == 0.3
    assert step._min_map_identity == 0.8


def test_degrade_loud_no_structure(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process(_bundle(structural_records=[])))
    sr = out["structural_reasoning"]
    assert sr["available"] is False
    assert "No loadable PDB" in sr["note"]
    rep = [r for r in out["stage_reports"] if r["stage"] == "structural_reasoning"]
    assert rep and "unavailable" in rep[0]["markdown"].lower()
    # Bundle passes through with the rest of the evidence intact.
    assert out["query"] == _bundle()["query"]


def test_degrade_loud_no_conserved_regions(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process(_bundle(conserved_regions=[])))
    sr = out["structural_reasoning"]
    assert sr["available"] is False
    assert sr["pdb_id"] == "3N40"
    assert "No conserved regions" in sr["note"]


def test_analyze_one_surfaces_container_traceback(tmp_path):
    """When the PyMOL job returns ok:false with a traceback, _analyze_one surfaces the REAL
    reason (note + traceback) — not a generic 'no usable result'."""
    step = _step(tmp_path)

    async def _fake_run_container(pdb_id, regions):
        return {
            "ok": False,
            "error_type": "ImportError",
            "note": "PyMOL job failed: ImportError: libGL.so.1: cannot open shared object file",
            "traceback": "Traceback (most recent call last):\n  ...\nImportError: libGL.so.1",
        }

    step._run_container = _fake_run_container  # type: ignore[method-assign]
    raw, note = asyncio.run(step._analyze_one("2XFB", []))
    assert raw is not None and raw["ok"] is False
    assert "libGL.so.1" in note  # the real reason, surfaced
    assert "Traceback (in PyMOL container)" in note  # the traceback is carried, not dropped


def test_degrade_loud_docker_unavailable(tmp_path, monkeypatch):
    """Docker/image missing -> named note + passthrough, never raises (G127)."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: False)
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]
    assert sr["available"] is False
    assert sr["pdb_id"] == "3N40"
    assert "not available" in sr["note"]
    assert [r for r in out["stage_reports"] if r["stage"] == "structural_reasoning"]


def _au_job_result(pdb_id: str) -> dict:
    """A realistic PyMOL-job result computed over the AU (no assembly available) — a data
    shape, not a mock of the PyMOL interface (real SASA parity is the 2XFB integration
    test ``test_no_assembly_degrades_to_au_named_caveat``)."""
    return {
        "ok": True,
        "pymol_version": "3.1.0",
        "pdb_id": pdb_id,
        "structure_kind": "asymmetric_unit",
        "assembly_id": None,
        "n_assembly_copies": 1,
        "neighbor_cutoff": None,
        "chain": "A",
        "chain_length": 100,
        "sasa_settings": {"dot_solvent": 1, "dot_density": 3},
        "n_conserved_regions": 1,
        "n_mapped_regions": 1,
        "n_mapped_residues": 1,
        "n_exposed": 1,
        "n_buried": 0,
        "exposed_residues": [
            {"resi": 50, "resn": "GLU", "state": "exposed", "rsa": 0.4, "sasa": 90.0}
        ],
        "buried_residues": [],
        "mapped_regions": [{"start": 0, "end": 4, "residues": [50]}],
        "contacts": [],
        "notes": [],
    }


def test_au_fallback_emits_named_caveat(tmp_path, monkeypatch):
    """E3-1.3 / CC-2: when SASA ran over the AU (no biological assembly in pdb1 format),
    the step stays available with a NON-EMPTY classification AND emits a NAMED caveat in
    both the bundle and the stage report — never a silent AU substitution."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)

    async def _au(self, pdb_id, regions):
        return _au_job_result(pdb_id)

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _au)
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]
    assert sr["available"] is True
    assert sr["structure_kind"] == "asymmetric_unit"
    assert sr["n_exposed"] == 1  # CC-1: non-empty real classification
    assert "asymmetric unit" in sr["assembly_caveat"]
    assert "3N40" in sr["assembly_caveat"]
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "asymmetric unit" in rep["markdown"]
    assert rep["data"]["structure_kind"] == "asymmetric_unit"
    assert rep["data"]["assembly_id"] is None


def test_assembly_context_named_in_report(tmp_path, monkeypatch):
    """The happy path (SASA over the biological assembly) names the assembly context in
    the stage report so the synthesis trace can cite it."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)

    async def _asm(self, pdb_id, regions):
        r = _au_job_result(pdb_id)
        r.update(
            structure_kind="assembly_1", assembly_id=1, n_assembly_copies=60, neighbor_cutoff=10.0
        )
        return r

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _asm)
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]
    assert sr["structure_kind"] == "assembly_1"
    assert sr["assembly_id"] == 1
    assert "assembly_caveat" not in sr
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "biological assembly 1" in rep["markdown"]
    assert rep["data"]["n_assembly_copies"] == 60


def test_container_failure_degrades_loud(tmp_path, monkeypatch):
    """A container/fetch error is caught and named, never propagated."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)

    async def _boom(self, pdb_id, regions):
        raise RuntimeError("simulated docker run failure")

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _boom)
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]
    assert sr["available"] is False
    assert "failed" in sr["note"]


# ----------------------------------------------------- R1: mmCIF large-assembly fetch path


class _GzipResp:
    """Minimal context-manager stand-in for ``urllib.request.urlopen`` returning gzip bytes
    (a data shape, not a mock of an interface — the bytes are real gzip the code decompresses)."""

    def __init__(self, payload: bytes):
        self._payload = gzip.compress(payload)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._payload


def _http_404(url, timeout=60):
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def test_fetch_prefers_legacy_pdb1_assembly(tmp_path, monkeypatch):
    """Happy path unchanged: a deposited legacy ``.pdb1`` assembly is used as ``assembly_1``."""
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda url, timeout=60: _GzipResp(b"PDB1\n"))
    path, kind = mod._fetch_structure("1ABC")
    assert kind == "assembly_1"
    assert path.name == "1ABC.pdb1"
    assert path.read_bytes() == b"PDB1\n"


def test_fetch_falls_back_to_mmcif_assembly_on_pdb1_404(tmp_path, monkeypatch):
    """R1: on a ``.pdb1`` 404 the mmCIF assembly is used (``mmcif_assembly``), NOT the AU —
    the large-assembly correctness fix. The AU fallback is NOT reached."""
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)
    monkeypatch.setattr(mod.urllib.request, "urlopen", _http_404)
    mmcif = tmp_path / "7K00.assembly1.cif"
    mmcif.write_text("data_7K00\n")
    monkeypatch.setattr(mod, "_fetch_mmcif_assembly", lambda pdb_id: mmcif)
    au_calls: list[str] = []
    monkeypatch.setattr(mod, "_fetch_au_cif", lambda pdb_id: au_calls.append(pdb_id))
    path, kind = mod._fetch_structure("7K00")
    assert kind == "mmcif_assembly"
    assert path == mmcif
    assert au_calls == []  # AU fallback NOT reached when an mmCIF assembly exists


def test_fetch_falls_back_to_au_only_when_no_assembly_anywhere(tmp_path, monkeypatch):
    """R1: AU fallback (``asymmetric_unit``) is reached ONLY when BOTH the .pdb1 AND the
    mmCIF assembly 404 — then (and only then) the AU caveat is accurate."""
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)
    monkeypatch.setattr(mod.urllib.request, "urlopen", _http_404)
    monkeypatch.setattr(mod, "_fetch_mmcif_assembly", lambda pdb_id: None)
    au = tmp_path / "1ABC.cif"
    au.write_text("data\n")
    monkeypatch.setattr(mod, "_fetch_au_cif", lambda pdb_id: au)
    path, kind = mod._fetch_structure("1ABC")
    assert kind == "asymmetric_unit"
    assert path == au


def test_fetch_mmcif_assembly_returns_none_on_404(tmp_path, monkeypatch):
    """A genuine no-assembly entry: the mmCIF assembly 404 → ``None`` so the caller degrades
    to the AU with an accurate (not misleading) caveat."""
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda url, timeout=120: _http_404(url))
    assert mod._fetch_mmcif_assembly("9ZZZ") is None


def test_fetch_mmcif_assembly_caches_decompressed(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda url, timeout=120: _GzipResp(b"data_7K00\n")
    )
    p = mod._fetch_mmcif_assembly("7K00")
    assert p is not None and p.name == "7K00.assembly1.cif"
    assert p.read_bytes() == b"data_7K00\n"


def test_fetch_mmcif_assembly_raises_on_non_404(tmp_path, monkeypatch):
    """A non-404 HTTP failure propagates (the caller degrades LOUD), never silently AU."""
    monkeypatch.setattr(mod, "_STRUCTURE_CACHE", tmp_path)

    def _500(url, timeout=120):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    monkeypatch.setattr(mod.urllib.request, "urlopen", _500)
    with pytest.raises(urllib.error.HTTPError):
        mod._fetch_mmcif_assembly("7K00")


def _mmcif_job_result(pdb_id: str) -> dict:
    """A realistic PyMOL-job result computed over an mmCIF biological assembly (R1) — a data
    shape (real-SASA parity is the 6N1D integration test)."""
    r = _au_job_result(pdb_id)
    r.update(structure_kind="mmcif_assembly", assembly_id=1, n_assembly_copies=1)
    return r


def test_mmcif_assembly_named_in_report_no_caveat(tmp_path, monkeypatch):
    """R1 / CC-2: SASA over the mmCIF assembly is a REAL assembly — it carries the assembly
    provenance (structure_kind=mmcif_assembly, assembly_id=1) and emits NO misleading
    'asymmetric unit / no assembly' caveat."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)

    async def _mmcif(self, pdb_id, regions):
        return _mmcif_job_result(pdb_id)

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _mmcif)
    out = asyncio.run(step.process(_bundle()))
    sr = out["structural_reasoning"]
    assert sr["available"] is True
    assert sr["structure_kind"] == "mmcif_assembly"
    assert sr["assembly_id"] == 1
    assert sr["n_exposed"] == 1  # CC-1: non-empty real classification
    assert "assembly_caveat" not in sr  # a real assembly → NOT an AU degrade
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "mmCIF" in rep["markdown"]
    assert rep["data"]["structure_kind"] == "mmcif_assembly"
    assert rep["data"]["assembly_id"] == 1


# ----------------------------------------------- E3-13: multi-structure step loop (offline)


def _multi_corpus() -> list[dict]:
    """Three loadable CHIKV E1/E2 records that rank DETERMINISTICALLY 3N40 > 2XFB > 6NK7
    (3N40 matches all four protein terms; 2XFB and 6NK7 tie on 2 and break by search rank)."""
    return [
        _struct_rec(
            "pdb:3N40",
            "Chikungunya envelope glycoprotein E1 E2 mature spike",
            ["ENVELOPE GLYCOPROTEIN", "E1", "E2"],
        ),
        _struct_rec("pdb:2XFB", "Chikungunya envelope glycoprotein", ["ENVELOPE GLYCOPROTEIN"]),
        _struct_rec("pdb:6NK7", "Chikungunya E1 glycoprotein", ["E1", "GLYCOPROTEIN"]),
    ]


def _job_for(pdb_id: str, exposed: list[int], buried: list[int], residues: list[int]) -> dict:
    """A realistic per-structure job result for the step loop (shared region cols 10-12)."""
    r = _au_job_result(pdb_id)
    r.update(
        chain="A",
        structure_kind="assembly_1",
        assembly_id=1,
        n_assembly_copies=60,
        n_mapped_regions=1,
        n_mapped_residues=len(residues),
        n_exposed=len(exposed),
        n_buried=len(buried),
        exposed_residues=[
            {"resi": x, "resn": "GLU", "state": "exposed", "rsa": 0.4, "sasa": 90.0}
            for x in exposed
        ],
        buried_residues=[
            {"resi": x, "resn": "LEU", "state": "buried", "rsa": 0.02, "sasa": 4.0} for x in buried
        ],
        mapped_regions=[{"start": 10, "end": 12, "consensus": "ABC", "residues": residues}],
    )
    return r


def test_step_analyzes_top_n_and_corroborates(tmp_path, monkeypatch):
    """The step analyses the top-N ranked structures and emits cross-structure corroboration;
    the PRIMARY structure still supplies the back-compat single-structure shape."""
    step = _step(tmp_path, max_structures=3)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)
    jobs = {
        "3N40": _job_for("3N40", exposed=[100, 101, 102], buried=[], residues=[100, 101, 102]),
        "2XFB": _job_for("2XFB", exposed=[200, 201], buried=[202], residues=[200, 201, 202]),
        "6NK7": _job_for("6NK7", exposed=[300], buried=[301, 302], residues=[300, 301, 302]),
    }

    async def _run(self, pdb_id, regions):
        return jobs[pdb_id]

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _run)
    out = asyncio.run(
        step.process(
            _bundle(structural_records=_multi_corpus(), protein="envelope glycoprotein E1 E2")
        )
    )
    sr = out["structural_reasoning"]

    assert sr["available"] is True
    assert sr["n_analyzed_structures"] == 3
    assert [s["pdb_id"] for s in sr["analyzed_structures"]] == ["3N40", "2XFB", "6NK7"]
    # PRIMARY = best-ranked success; its exposed_residues are the back-compat headline shape.
    assert sr["pdb_id"] == "3N40"
    assert [e["resi"] for e in sr["exposed_residues"]] == [100, 101, 102]
    # Corroboration: motif idx0 exposed in 3/3, idx1 in 2/3, idx2 in 1/3.
    by_idx = {c["motif_index"]: c for c in sr["corroboration"]}
    assert by_idx[0]["exposed_in_k"] == 3
    assert by_idx[1]["exposed_in_k"] == 2
    assert by_idx[2]["exposed_in_k"] == 1
    # Headline corroborated set anchored to the primary's residues (100->3/3, 101->2/3 majority).
    cr = {r["resi"]: r for r in sr["corroborated_residues"]}
    assert cr[100]["corroborated"] and cr[100]["exposed_in_k"] == 3
    assert cr[101]["corroborated"] and cr[101]["exposed_in_k"] == 2
    assert cr[102]["corroborated"] is False  # 1/3 minority
    assert sr["n_corroborated"] == 2
    # Stage report surfaces the multi-structure corroboration.
    rep = next(r for r in out["stage_reports"] if r["stage"] == "structural_reasoning")
    assert "Corroborated across 3 structures" in rep["markdown"]
    assert rep["data"]["n_analyzed_structures"] == 3
    assert rep["data"]["analyzed_pdb_ids"] == ["3N40", "2XFB", "6NK7"]


def test_step_per_structure_failure_skips_and_continues(tmp_path, monkeypatch):
    """A per-structure container failure DEGRADES LOUD (named, available=False in
    analyzed_structures) and the rest still aggregate — never strands the run."""
    step = _step(tmp_path, max_structures=3)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)
    jobs = {
        "3N40": _job_for("3N40", exposed=[100, 101], buried=[], residues=[100, 101]),
        "6NK7": _job_for("6NK7", exposed=[300], buried=[301], residues=[300, 301]),
    }

    async def _run(self, pdb_id, regions):
        if pdb_id == "2XFB":
            raise RuntimeError("simulated container OOM")
        return jobs[pdb_id]

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _run)
    out = asyncio.run(
        step.process(
            _bundle(structural_records=_multi_corpus(), protein="envelope glycoprotein E1 E2")
        )
    )
    sr = out["structural_reasoning"]
    assert sr["available"] is True
    assert sr["n_analyzed_structures"] == 2  # 2XFB skipped
    by_pdb = {s["pdb_id"]: s for s in sr["analyzed_structures"]}
    assert by_pdb["2XFB"]["available"] is False
    assert "simulated container OOM" in by_pdb["2XFB"]["note"]
    assert by_pdb["3N40"]["available"] is True and by_pdb["6NK7"]["available"] is True
    # The 2 surviving structures still corroborate.
    assert all(c["analyzed_n"] == 2 for c in sr["corroboration"])


def test_step_all_structures_fail_degrades_loud(tmp_path, monkeypatch):
    """All top-N fail -> the existing unavailable degrade, naming each per-structure failure."""
    step = _step(tmp_path, max_structures=2)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)

    async def _boom(self, pdb_id, regions):
        raise RuntimeError(f"boom-{pdb_id}")

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _boom)
    out = asyncio.run(
        step.process(
            _bundle(structural_records=_multi_corpus(), protein="envelope glycoprotein E1 E2")
        )
    )
    sr = out["structural_reasoning"]
    assert sr["available"] is False
    assert "All 2 candidate structure(s) failed" in sr["note"]
    assert sr["n_analyzed_structures"] == 0


def test_step_n1_reproduces_single_structure(tmp_path, monkeypatch):
    """N=1 (max_structures=1) reproduces the single-structure path: only the primary is
    analysed, exposed_residues are the primary's, and corroboration is 1/1."""
    step = _step(tmp_path, max_structures=1)
    monkeypatch.setattr(mod, "_docker_available", lambda image: True)
    jobs = {"3N40": _job_for("3N40", exposed=[100, 101], buried=[102], residues=[100, 101, 102])}

    seen: list[str] = []

    async def _run(self, pdb_id, regions):
        seen.append(pdb_id)
        return jobs[pdb_id]

    monkeypatch.setattr(StructuralReasoningStep, "_run_container", _run)
    out = asyncio.run(
        step.process(
            _bundle(structural_records=_multi_corpus(), protein="envelope glycoprotein E1 E2")
        )
    )
    sr = out["structural_reasoning"]
    assert seen == ["3N40"]  # only ONE container run despite 3 records
    assert sr["n_analyzed_structures"] == 1
    assert sr["pdb_id"] == "3N40"
    assert [e["resi"] for e in sr["exposed_residues"]] == [100, 101]
    # Corroboration over a single structure: each exposed position is exposed in 1/1.
    assert all(c["analyzed_n"] == 1 for c in sr["corroboration"])
    cr = {r["resi"]: r for r in sr["corroborated_residues"]}
    assert cr[100]["corroborated"] and cr[100]["exposed_in_k"] == 1


def test_env_overrides_max_structures(tmp_path, monkeypatch):
    """APECX_STRUCTURAL_MAX_STRUCTURES overrides the config default (ops knob)."""
    monkeypatch.setenv("APECX_STRUCTURAL_MAX_STRUCTURES", "1")
    step = _step(tmp_path, max_structures=3)
    assert step._max_structures == 1


# --------------------------------------------------------------- relevance ranking (P1)


def _struct_rec(subject: str, title: str, subjects: list[str], source: str = "pdb") -> dict:
    """A realistic Globus structural record (DataCite ``content`` shape)."""
    return {
        "subject": subject,
        "structural_source": source,
        "content": {
            "titles": [{"title": title}],
            "subjects": [{"subject": s} for s in subjects],
        },
    }


def _chikv_corpus() -> list[dict]:
    # Order mirrors a raw search rank where the capsid/protease structure happens to
    # rank first (the real 2CXD-vs-E1/E2 failure this fix targets).
    return [
        _struct_rec(
            "pdb:2CXD",
            "Chikungunya virus capsid protein C-terminal protease domain",
            ["CAPSID", "PROTEASE", "alphavirus", "chikungunya"],
        ),
        _struct_rec(
            "pdb:3N40",
            "Chikungunya virus envelope glycoprotein E1-E2 mature spike",
            ["ENVELOPE GLYCOPROTEIN", "E1", "E2", "VIRAL PROTEIN", "chikungunya"],
        ),
    ]


def test_ranking_prefers_surface_glycoprotein_over_capsid_protease():
    ranked = mod.rank_structural_records(_chikv_corpus(), protein="envelope glycoprotein E1 E2")
    # The surface glycoprotein record must outrank the capsid/protease record…
    assert ranked[0]["subject"] == "pdb:3N40"
    assert ranked[1]["subject"] == "pdb:2CXD"
    # …with a positive (surface + protein-match) score vs a negative (internal) score.
    assert ranked[0]["score"] > 0 > ranked[1]["score"]
    assert any("surface-antigen" in r for r in ranked[0]["reasons"])
    assert any("internal-protein" in r for r in ranked[1]["reasons"])


def test_ranking_without_protein_still_prefers_surface_over_internal():
    """Even with NO protein hint, surface vocabulary alone must beat internal vocabulary."""
    ranked = mod.rank_structural_records(_chikv_corpus(), protein=None)
    assert ranked[0]["subject"] == "pdb:3N40"


def test_ranking_no_signal_falls_back_to_search_rank():
    """No surface/internal/protein signal → stable order = upstream search rank (first loadable)."""
    recs = [
        _struct_rec("pdb:1AAA", "Some viral protein structure", ["VIRAL PROTEIN"]),
        _struct_rec("pdb:2BBB", "Another viral protein structure", ["VIRAL PROTEIN"]),
    ]
    ranked = mod.rank_structural_records(recs, protein=None)
    assert [r["subject"] for r in ranked] == ["pdb:1AAA", "pdb:2BBB"]
    assert all(r["score"] == 0 for r in ranked)


def test_step_selects_surface_structure_not_first_by_rank(tmp_path, monkeypatch):
    """END-OF-STAGE proof (docker stubbed unavailable so it degrades but still records the
    chosen structure): the step picks the E1/E2 envelope record, NOT the rank-0 capsid."""
    step = _step(tmp_path)
    monkeypatch.setattr(mod, "_docker_available", lambda image: False)
    bundle = _bundle(
        structural_records=_chikv_corpus(),
        protein="envelope glycoprotein E1 E2",
    )
    out = asyncio.run(step.process(bundle))
    sr = out["structural_reasoning"]
    assert sr["pdb_id"] == "3N40"  # the envelope glycoprotein, not 2CXD
    assert sr["selection"]["pdb_id"] == "3N40"
    rep = [r for r in out["stage_reports"] if r["stage"] == "structural_reasoning"][0]
    assert "Selected PDB 3N40" in rep["markdown"]
    assert "surface-antigen" in rep["markdown"]


def test_envelope_unwrap_and_non_dict_raises(tmp_path):
    step = _step(tmp_path)
    # Trigger-envelope shape {reasoning_input: bundle} is unwrapped.
    out = asyncio.run(step.process({"reasoning_input": _bundle(structural_records=[])}))
    assert out["structural_reasoning"]["available"] is False
    # A broken wiring contract (non-dict) is a real bug — fail loud.
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))
