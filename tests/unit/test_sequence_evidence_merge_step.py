"""Unit tests for SequenceEvidenceMergeStep — the sequence/structural fan-in (no network).

The merge folds the nested conserved-sites result into the evidence bundle and emits a
``sequence_conservation`` stage report. Two paths matter: the happy path (a real conservation
result read directly from the nested report dict's ``data.parts``) and the DEGRADE-LOUD path
(the sequence step returned a named-unavailable marker, or the data payload was missing/
malformed) — in which the run must still complete with a LOUD note, never a silent drop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps.sequence_conservation_subworkflow_step import (
    UNAVAILABLE_KEY,
)
from apecx_integration.composition.steps.sequence_evidence_merge_step import (
    SequenceEvidenceMergeStep,
)


def _stage(tmp_path: Path) -> SequenceEvidenceMergeStep:
    p = tmp_path / "merge.yml"
    p.write_text("name: merge_test\n")
    return SequenceEvidenceMergeStep.from_config(str(p))


def _structural_bundle() -> dict:
    # The shape StructuralEvidenceStep emits (query + a prior stage report).
    return {
        "query": "chikungunya structural polyprotein conserved epitopes",
        "publications": [],
        "globus_results": [],
        "structural_records": [],
        "structural_note": "No PDB or EMDB structural records were found.",
        "stage_reports": [
            {
                "stage": "context_assembly",
                "order": 1,
                "markdown": "assembled 3 branches",
                "data": {},
            },
            {"stage": "structural_evidence", "order": 2, "markdown": "no hits", "data": {}},
        ],
    }


def _conservation_result() -> dict:
    return {
        "n_sequences": 12,
        # Disclosure carry-forward (Phase 0): fetched-vs-used counts, per-strain records, the
        # aligned FASTA, and the per-column identity table flow from the conservation result.
        "n_fetched": 30,
        "n_dropped_length_outlier": 18,
        "records": [
            {
                "id": "fig|1.1.peg.1",
                "product": "E1",
                "genome_name": "CHIKV strain A",
                "sequence": "MK",
            },
            {
                "id": "fig|2.1.peg.1",
                "product": "E1",
                "genome_name": "CHIKV strain B",
                "sequence": "MK",
            },
        ],
        "alignment_fasta": ">a\nMK--\n>b\nMKL-\n",
        "per_column": [{"column": c, "identity": 0.9} for c in range(30)],
        "alignment_length": 30,
        "conservation_threshold": 0.9,
        "mean_identity": 0.95,
        "n_conserved_columns": 4,
        "conserved_sites": [
            {"column": 5, "consensus": "K", "identity": 1.0},
            {"column": 6, "consensus": "L", "identity": 0.92},
        ],
        "conserved_regions": [
            {"start": 5, "end": 6, "length": 2, "consensus": "KL", "mean_identity": 0.96},
            {"start": 10, "end": 13, "length": 4, "consensus": "MGAT", "mean_identity": 0.94},
        ],
    }


def _sequence_result(cons: dict) -> dict:
    """Mirror what the sequence SubworkflowStep returns on the happy path: the nested
    conserved-sites report dict ``{markdown, data:{kind:bundle, parts: conservation_result}}``."""
    return {
        "markdown": "# Conserved sites\n\n...",
        "data": {"kind": "bundle", "parts": cons},
    }


def test_merge_folds_real_conservation_and_emits_stage_report(tmp_path):
    cons = _conservation_result()
    inp = {
        "structural_in": _structural_bundle(),
        "sequence_in": _sequence_result(cons),
    }
    out = asyncio.run(_stage(tmp_path).process(inp))

    # Structured conservation threaded into the bundle for the later structural stage.
    assert out["conserved_regions"] == cons["conserved_regions"]
    assert out["conserved_sites"] == cons["conserved_sites"]
    # Phase-0 disclosure carry-forward: per-strain records, aligned FASTA, per-column table
    # land on the bundle (for the "Data actually used" section + the alignment viz + clade loop).
    assert out["sequence_used_records"] == cons["records"]
    assert out["alignment_fasta"] == cons["alignment_fasta"]
    assert len(out["per_column_conservation"]) == cons["alignment_length"]
    # Summary counts (NOT the heavy lists) reach the stage report.
    seq_data = {r["stage"]: r for r in out["stage_reports"]}["sequence_conservation"]["data"]
    assert seq_data["n_fetched"] == 30 and seq_data["n_used"] == 12
    assert seq_data["n_dropped_length_outlier"] == 18
    # The base structural bundle is preserved (query + prior keys carried through).
    assert out["query"] == _structural_bundle()["query"]
    assert out["structural_note"]

    # A sequence_conservation stage report (order 1) is appended, summarizing real regions + count.
    reports = {r["stage"]: r for r in out["stage_reports"]}
    assert "context_assembly" in reports and "structural_evidence" in reports  # prior reports kept
    seq_report = reports["sequence_conservation"]
    assert seq_report["order"] == 1
    assert "12 per-strain sequences" in seq_report["markdown"]
    assert "2 conserved region(s)" in seq_report["markdown"]
    assert seq_report["data"]["available"] is True
    assert seq_report["data"]["n_conserved_regions"] == 2


def test_merge_degrades_loud_on_unavailable_marker(tmp_path):
    inp = {
        "structural_in": _structural_bundle(),
        "sequence_in": {UNAVAILABLE_KEY: "no usable NCBI taxon_id on the query"},
    }
    out = asyncio.run(_stage(tmp_path).process(inp))

    # The run still completes; conservation keys present-but-empty (named, not silently absent).
    assert out["conserved_regions"] == []
    assert out["conserved_sites"] == []
    assert "no usable NCBI taxon_id" in out["sequence_conservation_note"]

    seq_report = {r["stage"]: r for r in out["stage_reports"]}["sequence_conservation"]
    assert seq_report["markdown"].startswith("Sequence conservation unavailable:")
    assert "no usable NCBI taxon_id" in seq_report["markdown"]
    assert seq_report["data"]["available"] is False


def test_merge_degrades_loud_on_missing_data_payload(tmp_path):
    # A sequence output that is neither a marker nor a conservation-data-bearing report.
    inp = {"structural_in": _structural_bundle(), "sequence_in": {"markdown": "x"}}
    out = asyncio.run(_stage(tmp_path).process(inp))
    assert out["conserved_regions"] == []
    assert "no conservation data payload" in out["sequence_conservation_note"]


def test_merge_degrades_loud_on_malformed_bundle(tmp_path):
    inp = {
        "structural_in": _structural_bundle(),
        "sequence_in": {"markdown": "x", "data": {"kind": "bundle", "parts": {"foo": 1}}},
    }
    out = asyncio.run(_stage(tmp_path).process(inp))
    assert out["conserved_regions"] == []
    assert "did not carry a conserved-sites bundle" in out["sequence_conservation_note"]


def test_merge_requires_structural_bundle(tmp_path):
    # A missing structural leg is a real wiring failure (the structural leg degrades loud but
    # always produces its bundle), so it must fail loud — not silently degrade.
    import pytest

    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"sequence_in": {UNAVAILABLE_KEY: "x"}}))
