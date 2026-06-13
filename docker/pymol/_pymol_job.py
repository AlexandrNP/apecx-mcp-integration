"""Headless PyMOL structural-reasoning job — runs INSIDE the containerized,
open-source PyMOL image (``apecx-pymol``), never in the host venv.

Invocation (from ``structural_reasoning_step.py`` via ``docker run``)::

    python /work/_pymol_job.py /work/job.json /work/result.json

It loads a pre-fetched structure file (the host downloads it from RCSB and mounts
it read-only into ``/work`` so the container needs NO network), maps each conserved
region's consensus motif onto the chain residues, computes PER-RESIDUE SASA with
PINNED settings (``dot_solvent=1``, ``dot_density=3``) via ``cmd.get_area`` in the
context of the loaded protein, classifies each conserved residue EXPOSED vs BURIED
(relative-SASA cutoff), and computes a CA–CA contact map over the mapped residues
with numpy. Determinism: the PyMOL version is recorded in the result, SASA settings
are pinned, residue order is the CA order from the structure, and all areas are
rounded — same structure + same conserved positions → byte-stable output.

PyMOL ``cmd.get_area`` SASA idiom (load → strip solvent/hetatm → pin dot_solvent/
dot_density → per-selection area) follows standard open-source PyMOL practice; the
agentic-pymol project (https://github.com/Arcadia-Science/agentic-pymol, MIT) uses
the same ``get_area`` primitive. Its GUI-socket delivery model is deliberately NOT
used here — this runs PyMOL fully headless/in-process.

The mapping + classification arithmetic lives in ``_pymol_sasa.py`` (pure, shared
with the host-side unit tests) so the numbers this job emits are produced by the
exact code the unit tests verify.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _pymol_sasa as sasa  # noqa: E402  (path set above)


def _pick_chain(cmd, obj: str, requested: str | None) -> str | None:
    chains = cmd.get_chains(obj) or []
    if requested:
        return requested if requested in chains else None
    for ch in chains:
        if cmd.count_atoms(f"{obj} and chain {ch} and name CA and polymer.protein") > 0:
            return ch
    return None


def _chain_sequence(cmd, obj: str, chain: str):
    """Ordered (resi, resn, 1-letter, ca_xyz) for the chain's CA atoms."""
    model = cmd.get_model(f"{obj} and chain {chain} and name CA and polymer.protein")
    resis: list = []
    resns: list[str] = []
    seq_chars: list[str] = []
    ca_xyz: list[list[float]] = []
    seen: set = set()
    for at in model.atom:
        key = (at.resi, at.segi)
        if key in seen:
            continue
        seen.add(key)
        resi = int(at.resi) if at.resi.lstrip("-").isdigit() else at.resi
        resis.append(resi)
        resns.append(at.resn)
        seq_chars.append(sasa.THREE_TO_ONE.get(at.resn.upper(), "X"))
        ca_xyz.append([float(c) for c in at.coord])
    return resis, resns, "".join(seq_chars), ca_xyz


def _contact_map(resi_list: list, ca_xyz: list[list[float]], cutoff: float) -> list[dict]:
    import numpy as np

    if len(resi_list) < 2:
        return []
    coords = np.asarray(ca_xyz, dtype=float)
    contacts: list[dict] = []
    for i in range(len(resi_list)):
        for j in range(i + 1, len(resi_list)):
            d = float(np.linalg.norm(coords[i] - coords[j]))
            if d <= cutoff:
                contacts.append({"a": resi_list[i], "b": resi_list[j], "distance": round(d, 3)})
    return contacts


def run(job: dict) -> dict:
    import pymol2

    structure_path = job["structure_path"]
    pdb_id = job.get("pdb_id")
    requested_chain = job.get("chain")
    conserved_regions = job.get("conserved_regions") or []
    rsa_threshold = float(job.get("rsa_threshold", 0.25))
    min_map_identity = float(job.get("min_map_identity", 0.7))
    contact_cutoff = float(job.get("contact_cutoff", 8.0))

    notes: list[str] = []

    with pymol2.PyMOL() as p:
        cmd = p.cmd
        version = str(cmd.get_version()[0])
        cmd.load(structure_path, "stru")
        cmd.remove("solvent")
        cmd.remove("hetatm")
        # PINNED SASA settings — determinism + reproducibility contract.
        cmd.set("dot_solvent", 1)
        cmd.set("dot_density", 3)

        chain = _pick_chain(cmd, "stru", requested_chain)
        if chain is None:
            return {
                "ok": False,
                "pymol_version": version,
                "pdb_id": pdb_id,
                "note": (
                    f"No protein chain with CA atoms found in {pdb_id} "
                    f"(requested chain={requested_chain!r})."
                ),
            }

        resis, resns, chain_seq, ca_xyz = _chain_sequence(cmd, "stru", chain)
        resn_by_resi = dict(zip(resis, resns, strict=True))
        ca_by_resi = dict(zip(resis, ca_xyz, strict=True))

        mapped_regions: list[dict] = []
        all_mapped_resis: list = []  # unique, first-seen order — overlapping regions share residues

        # First pass: map each region's motif onto chain residues; collect the UNIQUE set
        # of mapped residues across all regions (a residue covered by two overlapping
        # conserved regions is ONE residue, not two — dedup so SASA is computed once and the
        # exposed/buried lists carry no duplicate residue numbers).
        for region in conserved_regions:
            consensus = str(region.get("consensus", ""))
            motif = consensus.replace("-", "")
            mapping = sasa.map_motif_to_chain(
                motif, chain_seq, resis, min_identity=min_map_identity
            )
            if mapping is None:
                notes.append(
                    f"Conserved region (alignment cols {region.get('start')}–"
                    f"{region.get('end')}, motif {motif[:24]!r}) did not map onto chain "
                    f"{chain} of {pdb_id} at >= {min_map_identity:.0%} identity."
                )
                continue
            for r in mapping["residues"]:
                if r["resi"] not in all_mapped_resis:
                    all_mapped_resis.append(r["resi"])
            mapped_regions.append(
                {
                    "start": region.get("start"),
                    "end": region.get("end"),
                    "consensus": consensus,
                    "offset": mapping["offset"],
                    "map_identity": mapping["identity"],
                    "residues": [r["resi"] for r in mapping["residues"]],
                }
            )

        # Second pass: compute per-residue SASA + exposed/buried classification ONCE per
        # unique mapped residue.
        exposed: list[dict] = []
        buried: list[dict] = []
        for resi in all_mapped_resis:
            resn = resn_by_resi.get(resi, "")
            area = float(cmd.get_area(f"stru and chain {chain} and resi {resi}"))
            cls = sasa.classify_sasa(resn, area, rsa_threshold=rsa_threshold)
            entry = {"resi": resi, "resn": resn, **cls}
            if cls["state"] == "exposed":
                exposed.append(entry)
            elif cls["state"] == "buried":
                buried.append(entry)

        contact_xyz = [ca_by_resi[r] for r in all_mapped_resis]
        contacts = _contact_map(all_mapped_resis, contact_xyz, contact_cutoff)

    exposed_sorted = sorted(
        exposed,
        key=lambda e: (e["resi"] if isinstance(e["resi"], int) else 1 << 30, str(e["resi"])),
    )
    buried_sorted = sorted(
        buried, key=lambda e: (e["resi"] if isinstance(e["resi"], int) else 1 << 30, str(e["resi"]))
    )

    return {
        "ok": True,
        "pymol_version": version,
        "pdb_id": pdb_id,
        "chain": chain,
        "chain_length": len(chain_seq),
        "sasa_settings": {"dot_solvent": 1, "dot_density": 3},
        "rsa_threshold": rsa_threshold,
        "min_map_identity": min_map_identity,
        "contact_cutoff": contact_cutoff,
        "n_conserved_regions": len(conserved_regions),
        "n_mapped_regions": len(mapped_regions),
        "n_mapped_residues": len(all_mapped_resis),
        "n_exposed": len(exposed_sorted),
        "n_buried": len(buried_sorted),
        "exposed_residues": exposed_sorted,
        "buried_residues": buried_sorted,
        "mapped_regions": mapped_regions,
        "contacts": contacts,
        "notes": notes,
    }


def main() -> int:
    job_path, result_path = sys.argv[1], sys.argv[2]
    with open(job_path) as fh:
        job = json.load(fh)
    try:
        result = run(job)
    except Exception as exc:  # noqa: BLE001 — surface ANY failure as a result, never crash silently
        result = {"ok": False, "note": f"PyMOL job failed: {type(exc).__name__}: {exc}"}
    with open(result_path, "w") as fh:
        json.dump(result, fh, sort_keys=True, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
