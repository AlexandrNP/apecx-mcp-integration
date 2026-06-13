"""Pure structural-reasoning helpers for the ``structural_reasoning`` stage (E2-P).

These functions carry the *scientific* logic of the structural-reasoning stage —
mapping MSA-derived conserved regions onto a 3D structure's residues, and
classifying a residue EXPOSED vs BURIED from its solvent-accessible surface area
(SASA) — with ZERO dependency on PyMOL, numpy, or Docker. That keeps the logic
unit-testable in the plain venv: the same functions are also called *inside* the
containerized PyMOL job (``_pymol_job.py``), so a unit test of the classification
arithmetic exercises the exact code the integration run uses (mock/integration
parity — the SASA *numbers* are always real, only the per-residue SASA dict comes
from a fixture in unit tests vs real PyMOL ``cmd.get_area`` in the integration run).

Mapping model (deterministic, ungapped): a conserved region carries a gap-stripped
consensus *motif* (a contiguous peptide). We slide that motif over the structure
chain's 1-letter sequence and pick the offset with the highest per-position
identity (lowest offset on ties). A region maps only when the best identity clears
``min_identity`` — the structure is one specific strain, the consensus is a
cross-strain majority, so a small number of mismatches is expected and tolerated;
a region that does not clear the bar is reported LOUD as "not present in this
structure" rather than silently dropped.

Exposure model: relative solvent accessibility (RSA) = per-residue SASA divided by
the residue's theoretical maximum ASA (Tien et al. 2013, "Maximum allowed solvent
accessibilities of residues in proteins", PLoS ONE — the *theoretical* Gly-X-Gly
tripeptide set). A residue is EXPOSED when RSA >= ``rsa_threshold`` (0.25 is the
conventional cutoff). Epitopes are solvent-exposed, so the exposed conserved
residues are the candidate epitope residues.
"""

from __future__ import annotations

import re
from typing import Any

# Tien et al. 2013, theoretical (Gly-X-Gly) maximum allowed SASA per residue, Å².
# Keyed by 3-letter residue name (what PyMOL ``resn`` yields).
MAX_ASA_3: dict[str, float] = {
    "ALA": 129.0,
    "ARG": 274.0,
    "ASN": 195.0,
    "ASP": 193.0,
    "CYS": 167.0,
    "GLU": 223.0,
    "GLN": 225.0,
    "GLY": 104.0,
    "HIS": 224.0,
    "ILE": 197.0,
    "LEU": 201.0,
    "LYS": 236.0,
    "MET": 224.0,
    "PHE": 240.0,
    "PRO": 159.0,
    "SER": 155.0,
    "THR": 172.0,
    "TRP": 285.0,
    "TYR": 263.0,
    "VAL": 174.0,
}

# 3-letter → 1-letter (for deriving a chain sequence from residue names when a
# caller works from ``resn`` lists rather than a FASTA string).
THREE_TO_ONE: dict[str, str] = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLU": "E",
    "GLN": "Q",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
}

_PDB_ID_RE = re.compile(r"^[0-9][0-9a-zA-Z]{3}$")


def extract_pdb_id(record: dict[str, Any]) -> str | None:
    """Extract a 4-character PDB id from a structural record's ``subject``.

    Records look like ``{"subject": "pdb:1I9G", "structural_source": "pdb", ...}``.
    Only PDB records are usable (EMDB entries are density maps, not atomic
    coordinates PyMOL can load by id). Returns the upper-cased id or ``None`` when
    the record is not a loadable PDB entry.
    """
    if not isinstance(record, dict):
        return None
    # Explicitly non-PDB sources (e.g. emdb density maps) are not loadable as coordinates.
    if record.get("structural_source") == "emdb":
        return None
    subject = record.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        return None
    token = subject.strip()
    if ":" in token:
        prefix, _, rest = token.partition(":")
        if prefix.lower() not in ("pdb", ""):
            return None
        token = rest
    token = token.strip()
    if _PDB_ID_RE.match(token):
        return token.upper()
    return None


def select_candidate_pdb_id(records: list[dict[str, Any]]) -> str | None:
    """Pick the first loadable PDB id from the structural records (deterministic:
    list order, which is the upstream search rank). Returns ``None`` when none of
    the records yields a loadable PDB id."""
    if not isinstance(records, list):
        return None
    for rec in records:
        pid = extract_pdb_id(rec)
        if pid is not None:
            return pid
    return None


def map_motif_to_chain(
    motif: str,
    chain_seq: str,
    chain_resis: list[int],
    *,
    min_identity: float = 0.7,
) -> dict[str, Any] | None:
    """Map a conserved-region consensus *motif* onto chain residues (ungapped).

    Slides ``motif`` over ``chain_seq`` (same length as ``chain_resis``), scores
    each offset by per-position identity, and returns the best offset's mapping
    when its identity >= ``min_identity``. Tie-break: lowest offset (deterministic).

    Returns ``{"offset", "identity", "residues": [{"resi", "chain_aa", "motif_aa",
    "match"}]}`` or ``None`` (motif empty, longer than the chain, or below the
    identity bar — the caller reports that LOUD).
    """
    motif = (motif or "").strip().upper()
    if not motif:
        return None
    if len(chain_seq) != len(chain_resis):
        raise ValueError(
            f"map_motif_to_chain: chain_seq ({len(chain_seq)}) and chain_resis "
            f"({len(chain_resis)}) must be the same length."
        )
    span = len(motif)
    if span > len(chain_seq):
        return None

    best_offset = -1
    best_matches = -1
    for offset in range(0, len(chain_seq) - span + 1):
        matches = sum(1 for i in range(span) if chain_seq[offset + i] == motif[i])
        if matches > best_matches:
            best_matches = matches
            best_offset = offset
    identity = best_matches / span if span else 0.0
    if best_offset < 0 or identity < min_identity:
        return None

    residues = [
        {
            "resi": chain_resis[best_offset + i],
            "chain_aa": chain_seq[best_offset + i],
            "motif_aa": motif[i],
            "match": chain_seq[best_offset + i] == motif[i],
        }
        for i in range(span)
    ]
    return {"offset": best_offset, "identity": round(identity, 4), "residues": residues}


def relative_sasa(resn: str, sasa: float) -> float | None:
    """RSA = SASA / theoretical-max-ASA for the residue. ``None`` for non-standard
    residues (no reference max — e.g. a modified residue / ligand)."""
    max_asa = MAX_ASA_3.get((resn or "").upper())
    if not max_asa:
        return None
    return round(float(sasa) / max_asa, 4)


def classify_sasa(resn: str, sasa: float, *, rsa_threshold: float = 0.25) -> dict[str, Any]:
    """Classify one residue EXPOSED vs BURIED from its in-context SASA.

    ``{"state": "exposed"|"buried"|"unknown", "rsa": float|None, "sasa": float,
    "max_asa": float|None}``. ``unknown`` (never silently "buried") when the residue
    has no theoretical-max reference.
    """
    rsa = relative_sasa(resn, sasa)
    max_asa = MAX_ASA_3.get((resn or "").upper())
    state = "unknown" if rsa is None else ("exposed" if rsa >= rsa_threshold else "buried")
    return {
        "state": state,
        "rsa": rsa,
        "sasa": round(float(sasa), 3),
        "max_asa": max_asa,
    }


__all__ = [
    "MAX_ASA_3",
    "THREE_TO_ONE",
    "extract_pdb_id",
    "select_candidate_pdb_id",
    "map_motif_to_chain",
    "relative_sasa",
    "classify_sasa",
]
