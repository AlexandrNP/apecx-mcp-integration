"""Per-run provenance collection for viral_epitope_evidence_review (E3-8).

A single, reproducibility-oriented record that captures every determinism-relevant
parameter the science stages used on one run — so a reader can tell *exactly* what
produced a given result (and, where the inputs are pinned, re-derive it).

This is a COLLECTION task, not a re-computation: every value here is already recorded
by the stage that produced it and rides along in the evidence ``bundle`` dict that flows
step-to-step. :func:`collect_provenance` gathers those scattered records into ONE dict at
the ``review`` step (the last step that still holds the full bundle — downstream only the
rendered markdown survives), and the chain ``review → gate → envelope`` threads it into
``WorkflowResult.provenance``.

CC-1 (no empty responses): on a real, complete run every happy-path field is a NON-EMPTY
real value. A field that genuinely does not apply (no biological assembly deposited → no
``assembly_id``; a structure with no UniProt cross-reference → no ``uniprot_accessions``)
is an EXPLICIT named null (``None`` with the key present + a sibling ``available``/``note``
that names why), never a silently-missing key.

CC-2 / G127 (degrade-loud, never strand): collection NEVER raises. A malformed/partial
bundle yields named nulls, not an exception — a provenance failure must not sink a run
whose science already completed.
"""

from __future__ import annotations

import logging
from typing import Any

from apecx_integration.agents._llm_config import resolve_llm_model

log = logging.getLogger(__name__)

PROVENANCE_SCHEMA_VERSION = 1


def _stage_report_data(bundle: dict[str, Any], stage: str) -> dict[str, Any]:
    """The ``data`` payload of the named stage report, or ``{}`` when absent.

    Each reasoning stage appends a ``{stage, order, markdown, data}`` entry to
    ``bundle['stage_reports']`` (see ``_stage_report.py``); the machine-readable params
    live under ``data``.
    """
    reports = bundle.get("stage_reports")
    if not isinstance(reports, list):
        return {}
    for r in reports:
        if isinstance(r, dict) and r.get("stage") == stage:
            data = r.get("data")
            return data if isinstance(data, dict) else {}
    return {}


def _sequence_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    """Sequence-conservation determinism params: aligner + version, threshold, counts.

    Sourced from the ``sequence_conservation`` stage report's ``data`` (written by
    ``SequenceEvidenceMergeStep``). On a degraded sequence leg, names the absence.
    """
    data = _stage_report_data(bundle, "sequence_conservation")
    regions = bundle.get("conserved_regions")
    n_regions = len(regions) if isinstance(regions, list) else None
    if not data.get("available"):
        return {
            "available": False,
            "note": (
                data.get("note")
                or bundle.get("sequence_conservation_note")
                or "sequence-conservation stage did not run or produced no result"
            ),
            "aligner": None,
            "aligner_version": None,
            "conservation_threshold": None,
            "n_sequences": None,
            "n_conserved_regions": n_regions if n_regions is not None else 0,
        }
    return {
        "available": True,
        "note": None,
        "aligner": data.get("aligner"),
        "aligner_version": data.get("aligner_version"),
        "conservation_threshold": data.get("conservation_threshold"),
        "n_sequences": data.get("n_sequences"),
        "n_conserved_regions": (
            data.get("n_conserved_regions")
            if data.get("n_conserved_regions") is not None
            else (n_regions if n_regions is not None else 0)
        ),
    }


def _structural_retrieval_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    """The structural query actually ISSUED (E3-2): resolved organism spellings, the
    keyword query, and per-source hit counts — written into ``bundle['structural_query']``
    by ``StructuralEvidenceStep``.
    """
    meta = bundle.get("structural_query")
    note = bundle.get("structural_note")
    if not isinstance(meta, dict):
        return {
            "available": False,
            "note": note or "structural-retrieval stage did not record a query",
            "taxon_id": bundle.get("taxon_id"),
            "per_source": {},
        }
    per_source = meta.get("per_source")
    return {
        "available": bool(per_source),
        "note": note,
        "taxon_id": meta.get("taxon_id", bundle.get("taxon_id")),
        "per_source": per_source if isinstance(per_source, dict) else {},
    }


def _analyzed_structures_prov(sr: dict[str, Any]) -> list[dict[str, Any]]:
    """The N structures analysed for cross-structure corroboration (E3-13), as
    ``[{pdb_id, structure_kind, available}]``. Falls back to a single-entry list derived
    from the primary ``pdb_id``/``structure_kind`` for a single-structure (pre-E3-13)
    bundle, so the record is always a non-empty list when a structure was analysed.
    """
    analyzed = sr.get("analyzed_structures")
    if isinstance(analyzed, list) and analyzed:
        return [
            {
                "pdb_id": s.get("pdb_id"),
                "structure_kind": s.get("structure_kind"),
                "available": bool(s.get("available")),
            }
            for s in analyzed
            if isinstance(s, dict)
        ]
    if sr.get("pdb_id"):
        return [
            {
                "pdb_id": sr.get("pdb_id"),
                "structure_kind": sr.get("structure_kind"),
                "available": True,
            }
        ]
    return []


def _structural_reasoning_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    """The chosen PDB + ranking rationale, the assembly/SASA settings, exposed/buried
    counts — all already in ``bundle['structural_reasoning']`` (E3-1). E3-13 additionally
    records the N analysed structures (ids + per-structure kind). Names the absence when no
    structure was selected / PyMOL was unavailable.
    """
    sr = bundle.get("structural_reasoning")
    if not isinstance(sr, dict) or not sr.get("available"):
        note = sr.get("note") if isinstance(sr, dict) else None
        pdb = sr.get("pdb_id") if isinstance(sr, dict) else None
        analyzed = _analyzed_structures_prov(sr) if isinstance(sr, dict) else []
        return {
            "available": False,
            "note": note or "no structural-level reasoning was performed for this run",
            "pdb_id": pdb,
            "chain": None,
            "structure_kind": None,
            "assembly_id": None,
            "n_assembly_copies": None,
            "neighbor_cutoff": None,
            "pymol_version": None,
            "sasa_dot_solvent": None,
            "sasa_dot_density": None,
            "rsa_threshold": None,
            "contact_cutoff": None,
            "min_map_identity": None,
            "n_exposed": None,
            "n_buried": None,
            "ranking_rationale": [],
            "n_considered": None,
            "analyzed_structures": analyzed,
            "n_analyzed_structures": sr.get("n_analyzed_structures") if isinstance(sr, dict) else 0,
            "n_corroborated": None,
        }
    sasa = sr.get("sasa_settings") if isinstance(sr.get("sasa_settings"), dict) else {}
    sel = sr.get("selection") if isinstance(sr.get("selection"), dict) else {}
    analyzed = _analyzed_structures_prov(sr)
    return {
        "available": True,
        "note": sr.get("note"),
        "pdb_id": sr.get("pdb_id"),
        "chain": sr.get("chain"),
        "structure_kind": sr.get("structure_kind"),
        "assembly_id": sr.get("assembly_id"),
        "n_assembly_copies": sr.get("n_assembly_copies"),
        "neighbor_cutoff": sr.get("neighbor_cutoff"),
        "pymol_version": sr.get("pymol_version"),
        "sasa_dot_solvent": sasa.get("dot_solvent"),
        "sasa_dot_density": sasa.get("dot_density"),
        "rsa_threshold": sr.get("rsa_threshold"),
        "contact_cutoff": sr.get("contact_cutoff"),
        "min_map_identity": sr.get("min_map_identity"),
        "n_exposed": sr.get("n_exposed"),
        "n_buried": sr.get("n_buried"),
        "ranking_rationale": sel.get("reasons") or [],
        "n_considered": sel.get("considered"),
        "analyzed_structures": analyzed,
        "n_analyzed_structures": sr.get("n_analyzed_structures") or len(analyzed),
        "n_corroborated": sr.get("n_corroborated"),
    }


def _functional_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    """UniProt accession + release, SIFTS pdb_id, IEDB/UniProt query date, #coincidences
    — from ``bundle['functional_validation']`` (E3-3). When no structure with a UniProt
    cross-reference was selected, the UniProt/IEDB fields are named nulls.
    """
    fv = bundle.get("functional_validation")
    if not isinstance(fv, dict):
        return {
            "available": False,
            "note": "functional-validation stage did not run",
            "residue_level_annotation_available": False,
            "annotation_source": None,
            "uniprot_accessions": [],
            "uniprot_release": None,
            "sifts_pdb_id": None,
            "query_date": None,
            "n_uniprot_features": None,
            "n_iedb_epitope_spans": None,
            "n_coincidences": None,
            "n_candidate_epitope_residues": None,
        }
    real = bool(fv.get("residue_level_annotation_available"))
    sr = bundle.get("structural_reasoning")
    # SIFTS bridges the ANALYSED structure's PDB id → UniProt, so the SIFTS pdb id is the
    # structural-reasoning pdb id (the functional result does not re-store it). Only
    # meaningful when the real residue-level lookup actually ran.
    sifts_pdb = sr.get("pdb_id") if (real and isinstance(sr, dict) and sr.get("pdb_id")) else None
    coincidences = fv.get("coincidences")
    return {
        "available": True,
        "note": fv.get("annotation_note"),
        "residue_level_annotation_available": real,
        "annotation_source": fv.get("annotation_source"),
        "uniprot_accessions": fv.get("uniprot_accessions") or [],
        "uniprot_release": fv.get("uniprot_release"),
        "sifts_pdb_id": sifts_pdb,
        "query_date": fv.get("query_date"),
        "n_uniprot_features": fv.get("n_uniprot_features"),
        "n_iedb_epitope_spans": fv.get("n_iedb_epitope_spans"),
        "n_coincidences": len(coincidences) if isinstance(coincidences, list) else 0,
        "n_candidate_epitope_residues": fv.get("n_candidate_epitope_residues"),
    }


def collect_provenance(bundle: dict[str, Any]) -> dict[str, Any]:
    """Fold the determinism-relevant params recorded by the science stages into ONE record.

    Pure + total: reads only from ``bundle`` (the per-step evidence dict) and
    :func:`resolve_llm_model` (env-or-default, no network). NEVER raises — any extraction
    failure degrades the affected block to named nulls (CC-2 / G127). ``run_id`` is a named
    null here and is stamped post-run at the ``run_workflow`` seam (the run id is not known
    until the run completes).
    """
    if not isinstance(bundle, dict):
        bundle = {}
    try:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "run_id": bundle.get("run_id"),
            "llm_model": resolve_llm_model(),
            "inputs": {
                "query": bundle.get("query"),
                "taxon_id": bundle.get("taxon_id"),
                "protein": bundle.get("protein"),
            },
            "sequence_stage": _sequence_provenance(bundle),
            "structural_retrieval": _structural_retrieval_provenance(bundle),
            "structural_reasoning": _structural_reasoning_provenance(bundle),
            "functional_validation": _functional_provenance(bundle),
        }
    except Exception as exc:  # noqa: BLE001 — provenance MUST NOT strand a completed run
        log.warning(
            "collect_provenance: degraded to a minimal record (%s: %s)", type(exc).__name__, exc
        )
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "run_id": None,
            "llm_model": resolve_llm_model(),
            "note": f"provenance collection degraded: {type(exc).__name__}: {exc}",
            "inputs": {"query": None, "taxon_id": None, "protein": None},
            "sequence_stage": {"available": False, "note": "not collected"},
            "structural_retrieval": {"available": False, "note": "not collected"},
            "structural_reasoning": {"available": False, "note": "not collected"},
            "functional_validation": {"available": False, "note": "not collected"},
        }


__all__ = ["PROVENANCE_SCHEMA_VERSION", "collect_provenance"]
