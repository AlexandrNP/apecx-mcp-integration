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


def map_regions_on_chain(
    resis: list[Any],
    chain_seq: str,
    conserved_regions: list[dict[str, Any]],
    *,
    min_identity: float = 0.7,
    chain: str = "?",
    pdb_id: Any = None,
) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
    """Map every conserved region's consensus motif onto ONE chain's residues.

    Returns ``(mapped_regions, mapped_resis, notes)``: the mapped regions (each carrying
    this chain's author residue numbers + the map identity), the UNIQUE mapped residue set
    (first-seen order — overlapping regions share residues, so SASA is computed once and the
    exposed/buried lists carry no duplicate residue numbers), and a LOUD note per region that
    did NOT clear the identity bar on this chain (reported, never silently dropped). Pure
    (shared with the containerized PyMOL job) so the unit test exercises the exact arithmetic
    the integration run uses.
    """
    mapped_regions: list[dict[str, Any]] = []
    mapped_resis: list[Any] = []
    notes: list[str] = []
    for region in conserved_regions:
        consensus = str(region.get("consensus", ""))
        motif = consensus.replace("-", "")
        mapping = map_motif_to_chain(motif, chain_seq, resis, min_identity=min_identity)
        if mapping is None:
            notes.append(
                f"Conserved region (alignment cols {region.get('start')}–"
                f"{region.get('end')}, motif {motif[:24]!r}) did not map onto chain "
                f"{chain} of {pdb_id} at >= {min_identity:.0%} identity."
            )
            continue
        for r in mapping["residues"]:
            if r["resi"] not in mapped_resis:
                mapped_resis.append(r["resi"])
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
    return mapped_regions, mapped_resis, notes


def select_best_chain(
    per_chain: list[tuple[Any, list[Any], str]],
    conserved_regions: list[dict[str, Any]],
    *,
    min_identity: float = 0.7,
    pdb_id: Any = None,
) -> dict[str, Any] | None:
    """R3 (chain-pinning): from per-chain ``(chain, resis, chain_seq)`` sequences, pick the
    chain whose conserved motifs map with the BEST identity.

    Different deposits label the same biological protein differently (one PDB's E1 = chain F,
    another's = chain B; an antibody complex puts a Fab chain first), so a conserved region
    that maps onto the AUTO-PICKED chain in one structure but a non-first chain in another
    would not corroborate. Analysing the best-MAPPING chain pins the SAME conserved region
    across structures regardless of chain labelling, raising corroboration coverage. Score =
    ``(n_regions_mapped, sum_of_map_identities)``; ties resolve to the FIRST candidate
    (deterministic, back-compatible with the previous 'first protein chain' pick). When NO
    chain maps any region, the first candidate is returned so the loud 'no region mapped'
    note still names a concrete chain — it just does NOT corroborate (correct: the conserved
    protein is genuinely not present in that structure).

    Returns ``{chain, mapped_regions, mapped_resis, notes}`` for the winner, or ``None`` when
    ``per_chain`` is empty. Pure (shared with the containerized job)."""
    best: dict[str, Any] | None = None
    for chain, resis, chain_seq in per_chain:
        mapped_regions, mapped_resis, notes = map_regions_on_chain(
            resis,
            chain_seq,
            conserved_regions,
            min_identity=min_identity,
            chain=chain,
            pdb_id=pdb_id,
        )
        score = (len(mapped_regions), sum(m["map_identity"] for m in mapped_regions))
        if best is None or score > best["score"]:
            best = {
                "score": score,
                "chain": chain,
                "mapped_regions": mapped_regions,
                "mapped_resis": mapped_resis,
                "notes": notes,
            }
    if best is None:
        return None
    return {k: best[k] for k in ("chain", "mapped_regions", "mapped_resis", "notes")}


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


def _structure_positions(struct: dict[str, Any]) -> dict[tuple[Any, Any, int], dict[str, Any]]:
    """Map one structure's analysis onto SHARED conserved-region coordinates.

    The cross-structure correspondence key is ``(region_start, region_end, motif_index)``
    — the position of a residue WITHIN a conserved-region consensus motif, NOT its PDB
    author residue number. This is the scientifically correct shared coordinate: every
    analysed structure is fed the IDENTICAL ``conserved_regions`` (the same MSA-derived
    consensus motifs), so motif index *i* of region (start, end) denotes the SAME aligned
    biological position in every structure regardless of how each PDB happens to number
    its residues. ``struct`` is a PyMOL-job result (``mapped_regions`` + ``exposed_residues``
    + ``buried_residues``). Returns ``{key: {"resi", "state", "consensus_aa"}}``.
    """
    states: dict[Any, str] = {}
    for e in struct.get("exposed_residues") or []:
        if isinstance(e, dict) and "resi" in e:
            states[e["resi"]] = "exposed"
    for e in struct.get("buried_residues") or []:
        if isinstance(e, dict) and "resi" in e:
            states.setdefault(e["resi"], "buried")

    positions: dict[tuple[Any, Any, int], dict[str, Any]] = {}
    for reg in struct.get("mapped_regions") or []:
        if not isinstance(reg, dict):
            continue
        start, end = reg.get("start"), reg.get("end")
        motif = str(reg.get("consensus", "")).replace("-", "")
        residues = reg.get("residues") or []
        for i, resi in enumerate(residues):
            consensus_aa = motif[i] if i < len(motif) else "?"
            positions[(start, end, i)] = {
                "resi": resi,
                "state": states.get(resi, "unknown"),
                "consensus_aa": consensus_aa,
            }
    return positions


def aggregate_corroboration(
    per_structure: list[dict[str, Any]], *, threshold: float = 0.5
) -> list[dict[str, Any]]:
    """Corroborate candidate-epitope positions ACROSS the N analysed structures.

    For each shared conserved-region coordinate (see :func:`_structure_positions`), count
    in how many of the analysed structures the position mapped AND read solvent-EXPOSED.
    ``per_structure`` is the list of per-structure PyMOL-job results that SUCCEEDED (each
    a dict with ``ok`` / ``mapped_regions`` / ``exposed_residues`` / ``buried_residues`` /
    ``pdb_id``); failed structures are excluded by the caller (degrade-loud per structure).

    Returns a list (deterministically sorted by coordinate) of::

        {"region_start", "region_end", "motif_index", "consensus_aa",
         "exposed_in_k", "mapped_in_m", "analyzed_n",
         "exposed_pdb_ids": [...], "mapped_pdb_ids": [...],
         "resi_by_pdb": {pdb_id: resi}, "corroborated": bool}

    A position is ``corroborated`` when it is exposed in at least ``threshold`` (default
    0.5 = "at least half") of the analysed structures. For an odd N this is strict
    majority; pure + deterministic so the unit test verifies the exact arithmetic the
    integration run uses (mock/integration parity).
    """
    analyzed = [s for s in per_structure if isinstance(s, dict) and s.get("ok")]
    n = len(analyzed)
    agg: dict[tuple[Any, Any, int], dict[str, Any]] = {}
    for s in analyzed:
        pdb_id = s.get("pdb_id")
        for key, info in _structure_positions(s).items():
            entry = agg.setdefault(
                key,
                {
                    "region_start": key[0],
                    "region_end": key[1],
                    "motif_index": key[2],
                    "consensus_aa": info["consensus_aa"],
                    "mapped_in": [],
                    "exposed_in": [],
                    "resis": {},
                },
            )
            entry["mapped_in"].append(pdb_id)
            entry["resis"][pdb_id] = info["resi"]
            if info["state"] == "exposed":
                entry["exposed_in"].append(pdb_id)

    out: list[dict[str, Any]] = []
    for key in sorted(agg, key=lambda k: (str(k[0]), str(k[1]), k[2])):
        e = agg[key]
        k = len(e["exposed_in"])
        out.append(
            {
                "region_start": e["region_start"],
                "region_end": e["region_end"],
                "motif_index": e["motif_index"],
                "consensus_aa": e["consensus_aa"],
                "exposed_in_k": k,
                "mapped_in_m": len(e["mapped_in"]),
                "analyzed_n": n,
                "exposed_pdb_ids": sorted(p for p in e["exposed_in"] if p is not None),
                "mapped_pdb_ids": sorted(p for p in e["mapped_in"] if p is not None),
                "resi_by_pdb": dict(sorted(e["resis"].items(), key=lambda kv: str(kv[0]))),
                "corroborated": n > 0 and k >= 1 and (k / n) >= threshold,
            }
        )
    return out


def corroborated_residue_list(
    primary_struct: dict[str, Any], corroboration: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anchor the cross-structure corroboration onto the PRIMARY structure's residues.

    The headline ``exposed_residues`` (consumed by functional validation + SIFTS) stay the
    PRIMARY structure's residues — author-numbered against the PRIMARY ``pdb_id``. This adds,
    for each PRIMARY exposed residue, its "exposed in K of N structures" corroboration count
    keyed by the SHARED conserved-region coordinate. Returns a list sorted by ``resi`` of::

        {"resi", "consensus_aa", "exposed_in_k", "analyzed_n",
         "exposed_pdb_ids": [...], "corroborated": bool}
    """
    corr_by_key = {(c["region_start"], c["region_end"], c["motif_index"]): c for c in corroboration}
    # One entry per PRIMARY author residue (matching the deduped ``exposed_residues``
    # semantics): when two conserved-region positions map onto the same residue, keep the
    # MOST-corroborated (highest K) — deterministic on ties via the sorted key iteration.
    positions = _structure_positions(primary_struct)
    by_resi: dict[Any, dict[str, Any]] = {}
    for key in sorted(positions, key=lambda k: (str(k[0]), str(k[1]), k[2])):
        info = positions[key]
        if info["state"] != "exposed":
            continue
        c = corr_by_key.get(key)
        if not c:
            continue
        entry = {
            "resi": info["resi"],
            "consensus_aa": info["consensus_aa"],
            "exposed_in_k": c["exposed_in_k"],
            "analyzed_n": c["analyzed_n"],
            "exposed_pdb_ids": c["exposed_pdb_ids"],
            "corroborated": c["corroborated"],
        }
        prev = by_resi.get(info["resi"])
        if prev is None or entry["exposed_in_k"] > prev["exposed_in_k"]:
            by_resi[info["resi"]] = entry
    out = list(by_resi.values())
    out.sort(key=lambda e: (e["resi"] if isinstance(e["resi"], int) else 1 << 30, str(e["resi"])))
    return out


def assembly_exposure_flips(
    au_residues: list[dict[str, Any]], assembly_residues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Residues whose EXPOSED/BURIED verdict CHANGED between the asymmetric unit and the
    biological assembly (the load-bearing proof the assembly fix is real, not cosmetic).

    Both arguments are lists of classified residue dicts (``{"resi", "state", ...}`` as
    produced by ``classify_sasa`` + the PyMOL job) for the SAME residues computed in the
    two contexts. Returns, sorted deterministically by ``resi``, one
    ``{"resi", "au_state", "assembly_state"}`` per residue whose state differs (only
    where both contexts classified the residue — ``unknown`` non-standard residues and
    residues absent from one side are skipped, never silently counted as a flip).

    Pure (no PyMOL/Docker) so the AU-vs-assembly comparison the integration test asserts
    is computed by the exact arithmetic this unit test verifies (mock/integration parity).
    """
    au_state = {r.get("resi"): r.get("state") for r in au_residues}
    flips: list[dict[str, Any]] = []
    for r in assembly_residues:
        resi = r.get("resi")
        a_state = au_state.get(resi)
        b_state = r.get("state")
        if a_state in (None, "unknown") or b_state in (None, "unknown"):
            continue
        if a_state != b_state:
            flips.append({"resi": resi, "au_state": a_state, "assembly_state": b_state})
    flips.sort(key=lambda e: (e["resi"] if isinstance(e["resi"], int) else 1 << 30, str(e["resi"])))
    return flips


__all__ = [
    "MAX_ASA_3",
    "THREE_TO_ONE",
    "extract_pdb_id",
    "select_candidate_pdb_id",
    "map_motif_to_chain",
    "map_regions_on_chain",
    "select_best_chain",
    "relative_sasa",
    "classify_sasa",
    "aggregate_corroboration",
    "corroborated_residue_list",
    "assembly_exposure_flips",
]
