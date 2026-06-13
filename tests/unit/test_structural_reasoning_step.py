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
