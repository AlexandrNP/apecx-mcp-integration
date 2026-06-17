"""Pure helpers for the per-clade conservation analysis (Req 5 — broad effectiveness).

Two coordinate-distinct jobs, both PURE so they unit-test without a workflow:

* ``cluster_by_identity`` — group the ALIGNED strain sequences into clades by pairwise identity
  (greedy, deterministic). BV-BRC features carry no lineage field, so divergence is inferred
  from the sequences themselves; a homogeneous set yields one clade (an honest "N/A" signal).
* ``clade_conservation_breadth`` — per column, conservation WITHIN each clade summarized into
  pan-clade (same residue conserved in every clade → broad-spectrum) vs clade-restricted regions.
  This is the "broad effectiveness" signal and MUST be computed on the SHARED full alignment
  (every clade's rows are subsets of the same columns) — re-aligning each clade independently
  would give incomparable coordinates.
"""

from __future__ import annotations

from typing import Any


def pairwise_identity(a: str, b: str) -> float:
    """Identity over columns where BOTH sequences are non-gap. Returns 0.0 when there is no
    shared non-gap column (degrade-loud: maximally-divergent, not a crash)."""
    matches = 0
    comparable = 0
    for ca, cb in zip(a, b, strict=False):
        if ca == "-" or cb == "-":
            continue
        comparable += 1
        if ca == cb:
            matches += 1
    return (matches / comparable) if comparable else 0.0


def cluster_by_identity(
    aligned: list[tuple[str, str]],
    *,
    threshold: float = 0.95,
    min_size: int = 2,
) -> dict[str, Any]:
    """Greedy single-pass clustering of aligned sequences by pairwise identity.

    Deterministic: sequences are processed in their input order; each unclustered sequence seeds
    a clade and pulls in every still-unclustered sequence within ``threshold`` identity. Clades
    with fewer than ``min_size`` members are NOT separate clades (conservation needs >=2
    sequences) — their members are returned under ``ungrouped`` (named, never silently dropped).

    Returns ``{"clades": [[id, ...], ...], "ungrouped": [id, ...]}`` (clades sorted largest-first,
    ties by first member id for determinism)."""
    n = len(aligned)
    used = [False] * n
    raw_clades: list[list[int]] = []
    for i in range(n):
        if used[i]:
            continue
        members = [i]
        used[i] = True
        for j in range(i + 1, n):
            if used[j]:
                continue
            if pairwise_identity(aligned[i][1], aligned[j][1]) >= threshold:
                members.append(j)
                used[j] = True
        raw_clades.append(members)

    clades: list[list[str]] = []
    ungrouped: list[str] = []
    for members in raw_clades:
        ids = [aligned[k][0] for k in members]
        if len(ids) >= min_size:
            clades.append(ids)
        else:
            ungrouped.extend(ids)
    clades.sort(key=lambda ids: (-len(ids), ids[0] if ids else ""))
    return {"clades": clades, "ungrouped": ungrouped}


def _clade_column_consensus(
    alignment: dict[str, str], clade_member_ids: list[list[str]], length: int, threshold: float
) -> list[list[tuple[bool, str]]]:
    """For each clade, per-column ``(conserved_within_clade, consensus_residue)``.

    A column is conserved within a clade when the modal non-gap residue's frequency among that
    clade's rows is >= ``threshold``. Clades with <2 rows yield all-unconserved columns."""
    per_clade: list[list[tuple[bool, str]]] = []
    for ids in clade_member_ids:
        rows = [alignment[i] for i in ids if i in alignment]
        flags: list[tuple[bool, str]] = []
        if len(rows) < 2:
            per_clade.append([(False, "-")] * length)
            continue
        n = len(rows)
        for c in range(length):
            col = [r[c] for r in rows if c < len(r)]
            non_gap = [ch for ch in col if ch != "-"]
            if non_gap:
                # Deterministic modal residue: break frequency ties by residue letter (set
                # iteration is hash-seed-sensitive — a tie at a low threshold would otherwise
                # pick nondeterministically across processes).
                modal = max(set(non_gap), key=lambda ch: (non_gap.count(ch), ch))
                flags.append((non_gap.count(modal) / n >= threshold, modal))
            else:
                flags.append((False, "-"))
        per_clade.append(flags)
    return per_clade


def clade_conservation_breadth(
    alignment: list[tuple[str, str]],
    clade_member_ids: list[list[str]],
    *,
    identity_threshold: float = 0.9,
    min_region: int = 3,
) -> dict[str, Any]:
    """Broad-effectiveness across clades, computed on the SHARED alignment (correct coordinates).

    Per column, classify across clades:
      - PAN-CLADE: conserved within EVERY clade AND all clades share the SAME consensus residue
        → the same epitope is present in every clade (broad-spectrum candidate).
      - CLADE-RESTRICTED: conserved within >=1 clade but EITHER not in all, OR in all with a
        DIFFERING consensus (a mutated/clade-specific epitope — one vaccine won't cover all).

    NB: this deliberately does NOT take the pooled conserved_regions as input — those are
    conserved-by-construction across all strains, so they'd all read pan-clade (no signal). It
    re-derives conservation WITHIN each clade over every column, which is what reveals
    clade-restricted epitopes the pooled alignment masks.

    Returns ``{"available", "n_clades", "pan_clade_regions": [...],
    "clade_restricted_regions": [...], "n_pan_clade_columns", "alignment_length"}`` where each
    region is ``{"start", "end", "length", "consensus"|"per_clade_consensus"}``."""
    n_clades = len(clade_member_ids)
    if n_clades < 2 or not alignment:
        return {
            "available": False,
            "n_clades": n_clades,
            "pan_clade_regions": [],
            "clade_restricted_regions": [],
        }
    by_id = dict(alignment)
    length = len(alignment[0][1])
    per_clade = _clade_column_consensus(by_id, clade_member_ids, length, identity_threshold)

    # Per-column class: 2 = pan-clade, 1 = clade-restricted-conserved, 0 = not conserved anywhere.
    col_class: list[int] = []
    col_consensus: list[str] = []  # the shared residue for pan-clade columns
    col_clade_cons: list[list[str]] = []  # per-clade consensus (for restricted columns)
    for c in range(length):
        conserved_aas = [pc[c][1] for pc in per_clade if pc[c][0]]
        k = len(conserved_aas)
        if k == n_clades and len(set(conserved_aas)) == 1:
            col_class.append(2)
            col_consensus.append(conserved_aas[0])
        elif k >= 1:
            col_class.append(1)
            col_consensus.append("")
        else:
            col_class.append(0)
            col_consensus.append("")
        col_clade_cons.append([pc[c][1] if pc[c][0] else "." for pc in per_clade])

    pan = _runs(col_class, 2, min_region, lambda a, b: "".join(col_consensus[a : b + 1]))
    restricted = _runs(
        col_class,
        1,
        min_region,
        lambda a, b: [
            "".join(col_clade_cons[c][ci] for c in range(a, b + 1)) for ci in range(n_clades)
        ],
        consensus_key="per_clade_consensus",
    )
    return {
        "available": True,
        "n_clades": n_clades,
        "alignment_length": length,
        "pan_clade_regions": pan,
        "clade_restricted_regions": restricted,
        "n_pan_clade_columns": sum(1 for x in col_class if x == 2),
    }


def _runs(
    col_class: list[int],
    target: int,
    min_region: int,
    consensus_fn,
    *,
    consensus_key: str = "consensus",
) -> list[dict[str, Any]]:
    """Contiguous runs of columns whose class == target, each >= min_region long."""
    out: list[dict[str, Any]] = []
    start = None
    n = len(col_class)
    for c in range(n + 1):
        is_target = c < n and col_class[c] == target
        if is_target and start is None:
            start = c
        elif not is_target and start is not None:
            end = c - 1
            if end - start + 1 >= min_region:
                out.append(
                    {
                        "start": start,
                        "end": end,
                        "length": end - start + 1,
                        consensus_key: consensus_fn(start, end),
                    }
                )
            start = None
    return out


__all__ = ["clade_conservation_breadth", "cluster_by_identity", "pairwise_identity"]
