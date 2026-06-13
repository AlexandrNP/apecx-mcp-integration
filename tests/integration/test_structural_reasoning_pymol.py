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


def _require_rcsb() -> None:
    """Runtime (not import-time) network check, so a transient blip skips THIS test
    instead of stalling collection of the whole file for the urlopen timeout."""
    try:
        with urllib.request.urlopen("https://files.rcsb.org/download/3N40.cif", timeout=15) as r:
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
