"""Residue-level annotation orchestration + numbering bridge + cross-check (E3-3.4).

Glues the three clients together for the FunctionalValidationStep:

1. SIFTS: PDB id + chosen chain → per-chain mapping segments (accession + author→UniProt
   offset).
2. UniProt: for each accession on that chain → residue features + canonical sequence.
3. IEDB: for each accession → linear epitopes, located in the UniProt sequence to get
   spans in the SAME UNIPROT coordinate frame as the features.

The output is an ``annotation context`` consumed by :func:`cross_check_residues`, a pure
function that maps each candidate AUTHOR residue to its UniProt position and tests
membership in the feature / epitope spans. Both the gather and the cross-check degrade
loud (named note), never raise on content — the step must never strand the cascade (G127).
"""

from __future__ import annotations

import logging
from typing import Any

from apecx_integration.agents.functional import sifts_client
from apecx_integration.agents.functional.iedb_client import IedbClient
from apecx_integration.agents.functional.sifts_client import SiftsClient
from apecx_integration.agents.functional.uniprot_client import UniProtClient

log = logging.getLogger(__name__)

# UniProt "bonded-pair" feature types: ``start``/``end`` are the TWO residues joined by a
# bond, NOT a contiguous span. A disulfide "858..923" means Cys858–Cys923, so only those
# two residues participate — treating it as the range [858,923] would falsely mark every
# residue between them as coinciding (a real bug caught on 2XFB/Q1H8W5, 2026-06-13).
_BOND_TYPES = frozenset({"Disulfide bond", "Cross-link"})


def feature_covers(feature: dict[str, Any], unp_pos: int) -> bool:
    """True when ``unp_pos`` participates in ``feature``.

    Range features (Domain, Glycosylation, Active/Binding site, Site) cover the inclusive
    span ``[start, end]``; bonded-pair features (Disulfide, Cross-link) cover only the two
    endpoint residues ``{start, end}``.
    """
    start, end = feature["start"], feature["end"]
    if feature["type"] in _BOND_TYPES:
        return unp_pos in (start, end)
    return start <= unp_pos <= end


def locate_epitope_spans(uniprot_seq: str, epitope_seq: str) -> list[tuple[int, int]]:
    """Return every (1-based start, end) span where ``epitope_seq`` occurs in ``uniprot_seq``.

    An IEDB linear epitope carries no positions; we locate it in the UniProt canonical
    sequence to obtain spans in the same frame as the UniProt features. May return multiple
    spans (repeated motif) or none (epitope from a variant/isoform not in this sequence).
    """
    if not uniprot_seq or not epitope_seq:
        return []
    spans: list[tuple[int, int]] = []
    start = uniprot_seq.find(epitope_seq)
    while start != -1:
        spans.append((start + 1, start + len(epitope_seq)))
        start = uniprot_seq.find(epitope_seq, start + 1)
    return spans


async def gather_annotation_context(
    pdb_id: str,
    chain: str,
    *,
    sifts: SiftsClient,
    uniprot: UniProtClient,
    iedb: IedbClient,
) -> dict[str, Any]:
    """Fetch SIFTS + UniProt + IEDB annotation for ``pdb_id`` chain ``chain``.

    Returns ``{available: True, ...}`` with the segments, features and located IEDB spans,
    or ``{available: False, note: <named reason>}`` for any degrade (no UniProt xref,
    network failure). Never raises.
    """
    try:
        mappings = await sifts.get_mappings(pdb_id)
    except Exception as exc:  # noqa: BLE001 — degrade loud, never strand the cascade
        return _degrade(f"SIFTS lookup for {pdb_id} failed ({type(exc).__name__}: {exc})")

    if not mappings:
        return _degrade(
            f"PDB {pdb_id} has no UniProt cross-reference in SIFTS; residue-level "
            "functional annotation cannot be cross-referenced for this structure"
        )

    segments = sifts_client.chain_segments(mappings, chain)
    if not segments:
        return _degrade(
            f"SIFTS has UniProt mappings for {pdb_id} but none cover chain {chain}; "
            "no residue bridge available for the analysed chain"
        )

    accessions = sorted({seg["accession"] for seg in segments})
    features_by_acc: dict[str, list[dict[str, Any]]] = {}
    iedb_spans_by_acc: dict[str, list[dict[str, Any]]] = {}
    releases: dict[str, str] = {}
    query_dates: dict[str, str] = {}
    n_iedb = 0
    iedb_notes: list[str] = []

    for acc in accessions:
        try:
            entry = await uniprot.get_entry(acc)
        except Exception as exc:  # noqa: BLE001
            return _degrade(
                f"UniProt lookup for {acc} (from {pdb_id}) failed ({type(exc).__name__}: {exc})"
            )
        if not entry:
            features_by_acc[acc] = []
            iedb_spans_by_acc[acc] = []
            iedb_notes.append(f"UniProt has no entry for {acc}")
            continue
        features_by_acc[acc] = entry["features"]
        releases[acc] = entry["release"]
        query_dates[acc] = entry["query_date"]

        # IEDB is a bonus source; its failure must not sink the whole context.
        spans: list[dict[str, Any]] = []
        try:
            epitopes = await iedb.search_epitopes(acc)
        except Exception as exc:  # noqa: BLE001
            iedb_notes.append(f"IEDB lookup for {acc} failed ({type(exc).__name__})")
            epitopes = []
        if not epitopes:
            iedb_notes.append(f"IEDB: no linear epitopes for {acc}")
        for ep in epitopes:
            for s, e in locate_epitope_spans(entry["sequence"], ep["linear_sequence"]):
                spans.append({"sequence": ep["linear_sequence"], "start": s, "end": e})
                n_iedb += 1
        iedb_spans_by_acc[acc] = spans

    return {
        "available": True,
        "pdb_id": pdb_id,
        "chain": chain,
        "accessions": accessions,
        "segments": segments,
        "features_by_acc": features_by_acc,
        "iedb_spans_by_acc": iedb_spans_by_acc,
        "uniprot_release": "; ".join(f"{a}:{releases.get(a, '?')}" for a in accessions),
        "query_date": next(iter(query_dates.values()), None),
        "n_uniprot_features": sum(len(v) for v in features_by_acc.values()),
        "n_iedb_epitope_spans": n_iedb,
        "iedb_notes": iedb_notes,
        "note": None,
    }


def cross_check_residues(candidate_resis: list[int], ctx: dict[str, Any]) -> dict[str, Any]:
    """Cross-check candidate AUTHOR residues against the annotation context.

    Returns ``{coincidences, residue_findings}``:

    - ``coincidences`` — structured hits (one per overlapping feature / epitope span).
    - ``residue_findings`` — a per-residue human-readable line, ALWAYS non-empty when the
      context is available (a coincidence line, or an explicit "no feature at residue N").
      This guarantees CC-1: a context that says ``available`` never yields an empty
      coincidences list with no named per-residue absence.

    Pure function (no I/O) so the bridge logic is unit-testable offline.
    """
    segments = ctx.get("segments", [])
    features_by_acc = ctx.get("features_by_acc", {})
    iedb_spans_by_acc = ctx.get("iedb_spans_by_acc", {})

    coincidences: list[dict[str, Any]] = []
    findings: list[str] = []

    if not candidate_resis:
        accs = ", ".join(ctx.get("accessions", [])) or "the chain's antigen"
        findings.append(
            f"Residue-level annotation was fetched for {accs} "
            f"({ctx.get('n_uniprot_features', 0)} UniProt feature(s), "
            f"{ctx.get('n_iedb_epitope_spans', 0)} IEDB epitope span(s)), but no "
            "structure-derived candidate residues were available to cross-reference."
        )
        return {"coincidences": coincidences, "residue_findings": findings}

    for resi in candidate_resis:
        bridged = sifts_client.bridge_residue(segments, resi)
        if bridged is None:
            findings.append(
                f"residue {resi}: outside the UniProt-mapped region of chain "
                f"{ctx.get('chain')} — no residue bridge, cannot cross-reference"
            )
            continue
        acc, unp = bridged
        hits: list[str] = []
        for feat in features_by_acc.get(acc, []):
            if feature_covers(feat, unp):
                desc = feat["description"] or feat["type"]
                hits.append(
                    f"residue {resi} (UniProt {acc}:{unp}) coincides with {feat['type']}: {desc}"
                )
                coincidences.append(
                    {
                        "residue": resi,
                        "unp_pos": unp,
                        "accession": acc,
                        "source": "UniProt",
                        "type": feat["type"],
                        "description": feat["description"],
                        "feature_start": feat["start"],
                        "feature_end": feat["end"],
                    }
                )
        for span in iedb_spans_by_acc.get(acc, []):
            if span["start"] <= unp <= span["end"]:
                hits.append(
                    f"residue {resi} (UniProt {acc}:{unp}) within IEDB epitope "
                    f"{span['sequence']} ({span['start']}-{span['end']})"
                )
                coincidences.append(
                    {
                        "residue": resi,
                        "unp_pos": unp,
                        "accession": acc,
                        "source": "IEDB",
                        "epitope": span["sequence"],
                        "epitope_start": span["start"],
                        "epitope_end": span["end"],
                    }
                )
        if hits:
            findings.extend(hits)
        else:
            findings.append(
                f"no functional/immunological feature at residue {resi} (UniProt {acc}:{unp})"
            )

    return {"coincidences": coincidences, "residue_findings": findings}


def _degrade(note: str) -> dict[str, Any]:
    log.info("residue_annotation degrade: %s", note)
    return {"available": False, "note": note}


__all__ = [
    "gather_annotation_context",
    "cross_check_residues",
    "locate_epitope_spans",
    "feature_covers",
]
