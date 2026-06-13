"""Headless PyMOL structural-reasoning job — runs INSIDE the containerized,
open-source PyMOL image (``apecx-pymol``), never in the host venv.

Invocation (from ``structural_reasoning_step.py`` via ``docker run``)::

    python /work/_pymol_job.py /work/job.json /work/result.json

It loads a pre-fetched structure file (the host downloads it from RCSB and mounts
it read-only into ``/work`` so the container needs NO network). When that file is the
BIOLOGICAL ASSEMBLY (``.pdb1``, the functional oligomer), the symmetry-mate copies —
which RCSB encodes as PDB ``MODEL`` records / PyMOL STATES — are co-located into one
single-state object (``_assembly_context``) so per-residue SASA is occluded by the
WHOLE oligomer, not just the deposited asymmetric unit: an interface residue that
reads solvent-exposed on the AU is correctly buried in the assembly. It then maps each
conserved region's consensus motif onto the original copy's chain residues, computes
PER-RESIDUE SASA with PINNED settings (``dot_solvent=1``, ``dot_density=3``) via a
single ``cmd.get_area(..., load_b=1)`` pass in the assembly context, classifies each
conserved residue EXPOSED vs BURIED (relative-SASA cutoff), and computes a CA–CA
contact map over the mapped residues with numpy. Determinism: the PyMOL version +
assembly id are recorded in the result, SASA settings are pinned, residue order is the
CA order from the structure, and all areas are rounded — same structure + same
conserved positions → byte-stable output.

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

# Å. A solvent dot on the original copy can only be occluded by an atom within roughly
# (2*probe + 2*vdw) ≈ 9 Å of it, so per-residue SASA of the original copy in the
# (original + neighbour-shell) context is IDENTICAL to the full biological assembly —
# letting us prune away the rest of a large assembly (2XFB = 60 copies, ~1.35M atoms)
# that would make per-residue cmd.get_area prohibitively slow while changing no number.
_NEIGHBOR_CUTOFF = 10.0


def _assembly_context(cmd, raw_obj: str, work_obj: str, cutoff: float) -> tuple[str, int]:
    """Build the SASA-context object for the biological assembly and return
    ``(base_selection, n_copies)``.

    RCSB legacy biological-assembly files (``.pdb1``) encode each assembly copy as a
    PDB ``MODEL`` — which PyMOL loads as a separate STATE. ``cmd.get_area`` only sees
    the CURRENT state, so a multi-state object would read as a single copy (≈ the
    asymmetric unit) — the exact silent no-op this stage exists to avoid. We split the
    states, tag each copy with a unique ``segi`` (``C0`` = the ORIGINAL deposited copy,
    which retains author numbering), and merge them into one single-state ``_full``
    object so the whole oligomer is co-located in one frame.

    We then PRUNE ``_full`` to the original copy plus only the symmetry-mate atoms within
    ``cutoff`` of it (occlusion is local — see ``_NEIGHBOR_CUTOFF``): ``cmd.get_area`` over
    that lean ``work_obj`` yields per-residue SASA of the original copy IN the assembly
    context, identical to the full assembly, without the cost of surfacing ~1.35M atoms
    per residue. ``base_selection`` selects only the original copy (segi ``C0``); the
    returned ``n_copies`` is the assembly's TOTAL copy count (provenance). Single-state
    inputs (the AU ``.cif``, or a single-MODEL assembly) pass through unchanged.
    """
    n_states = cmd.count_states(raw_obj)
    if n_states <= 1:
        cmd.set_name(raw_obj, work_obj)
        return work_obj, 1
    cmd.split_states(raw_obj, prefix="_cp")
    cmd.delete(raw_obj)
    copies = sorted(cmd.get_object_list("_cp*"))
    for i, obj in enumerate(copies):
        cmd.alter(obj, f"segi='C{i}'")
    cmd.sort()
    cmd.create("_full", " or ".join(copies))
    cmd.delete("_cp*")
    cmd.create(
        work_obj,
        f"(_full and segi C0) or "
        f"byres ((_full and not segi C0) within {cutoff} of (_full and segi C0))",
    )
    cmd.delete("_full")
    cmd.sort()
    return f"{work_obj} and segi C0", len(copies)


def _pick_chain(cmd, base_sel: str, requested: str | None) -> str | None:
    chains = cmd.get_chains(base_sel) or []
    if requested:
        return requested if requested in chains else None
    for ch in chains:
        if cmd.count_atoms(f"({base_sel}) and chain {ch} and name CA and polymer.protein") > 0:
            return ch
    return None


def _chain_sequence(cmd, base_sel: str, chain: str):
    """Ordered (resi, resn, 1-letter, ca_xyz) for the chain's CA atoms (original copy)."""
    model = cmd.get_model(f"({base_sel}) and chain {chain} and name CA and polymer.protein")
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


def _per_residue_area(cmd, base_sel: str, chain: str) -> dict:
    """Sum the b-factor-loaded per-atom SASA into per-residue totals for the original
    copy's chain (call after ``cmd.get_area('work', load_b=1)``)."""
    model = cmd.get_model(f"({base_sel}) and chain {chain} and polymer.protein")
    area_by_resi: dict = {}
    for at in model.atom:
        resi = int(at.resi) if at.resi.lstrip("-").isdigit() else at.resi
        area_by_resi[resi] = area_by_resi.get(resi, 0.0) + float(at.b)
    return area_by_resi


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
    # 'assembly_1' = the legacy-PDB biological assembly (functional oligomer);
    # 'mmcif_assembly' = the same biological assembly in mmCIF, used when the assembly is
    # too large for the legacy PDB format (R1: ribosomes, multi-assembly crystals — the
    # mmCIF assembly already carries every chain of assembly 1); 'asymmetric_unit' = the
    # deposited AU (fallback only when NO assembly is deposited in either format). SASA
    # over the assembly is the correct accessibility for an oligomeric antigen — interface
    # residues that read EXPOSED in the AU are BURIED once the assembly's copies/chains
    # are present.
    structure_kind = job.get("structure_kind", "asymmetric_unit")
    requested_chain = job.get("chain")
    conserved_regions = job.get("conserved_regions") or []
    rsa_threshold = float(job.get("rsa_threshold", 0.25))
    min_map_identity = float(job.get("min_map_identity", 0.7))
    contact_cutoff = float(job.get("contact_cutoff", 8.0))

    notes: list[str] = []
    assembly_id = 1 if structure_kind in ("assembly_1", "mmcif_assembly") else None

    with pymol2.PyMOL() as p:
        cmd = p.cmd
        version = str(cmd.get_version()[0])
        # Legacy assembly files (.pdb1) carry no recognised extension — force PDB format.
        # The mmCIF assembly (.cif) and the AU (.cif) auto-detect from the extension.
        if structure_kind == "assembly_1":
            cmd.load(structure_path, "raw", format="pdb")
        else:
            cmd.load(structure_path, "raw")
        cmd.remove("raw and solvent")
        cmd.remove("raw and hetatm")
        # PINNED SASA settings — determinism + reproducibility contract.
        cmd.set("dot_solvent", 1)
        cmd.set("dot_density", 3)
        # Build the assembly-context object: get_area over `work` occludes the original
        # copy's residues with the assembly's symmetry-mate neighbours (see docstring).
        base_sel, n_copies = _assembly_context(cmd, "raw", "work", _NEIGHBOR_CUTOFF)

        chain = _pick_chain(cmd, base_sel, requested_chain)
        if chain is None:
            return {
                "ok": False,
                "pymol_version": version,
                "pdb_id": pdb_id,
                "structure_kind": structure_kind,
                "assembly_id": assembly_id,
                "n_assembly_copies": n_copies,
                "note": (
                    f"No protein chain with CA atoms found in {pdb_id} "
                    f"(requested chain={requested_chain!r})."
                ),
            }

        resis, resns, chain_seq, ca_xyz = _chain_sequence(cmd, base_sel, chain)
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

        # ONE surface computation loads each atom's in-context SASA into its b-factor
        # (occluded by the whole `work` object — i.e. the original copy PLUS the
        # assembly's neighbour shell). Per-residue SASA is then the sum of its atoms'
        # areas — identical to one get_area per residue, but a single surface calc
        # instead of one per residue (decisive on a large assembly context).
        cmd.get_area("work", load_b=1)
        per_residue_area = _per_residue_area(cmd, base_sel, chain)

        # Second pass: classify each unique mapped residue EXPOSED vs BURIED.
        exposed: list[dict] = []
        buried: list[dict] = []
        for resi in all_mapped_resis:
            resn = resn_by_resi.get(resi, "")
            area = float(per_residue_area.get(resi, 0.0))
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
        "structure_kind": structure_kind,
        "assembly_id": assembly_id,
        "n_assembly_copies": n_copies,
        "neighbor_cutoff": _NEIGHBOR_CUTOFF if structure_kind == "assembly_1" else None,
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
