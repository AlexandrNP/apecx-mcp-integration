"""Unit tests for the E3-8 per-run provenance record.

Two layers:

1. ``collect_provenance`` over a realistic bundle → every expected field NON-EMPTY
   (CC-1); over a partial/garbage bundle → explicit named nulls, never a missing key,
   never a crash (CC-2 / G127).
2. The seam threading: ``review → gate → envelope`` carries the record into a
   ``WorkflowResult.provenance`` dict (no live LLM — synthesis monkeypatched).
"""

from __future__ import annotations

import asyncio

from apecx_integration.composition.steps import evidence_review_synthesis_step as review_mod
from apecx_integration.composition.steps._evidence_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    collect_provenance,
)


def _full_bundle() -> dict:
    """A realistic bundle as it reaches the review step on a complete CHIKV run:
    every science stage populated its block."""
    return {
        "query": "conserved chikungunya E1 epitopes",
        "taxon_id": 37124,
        "protein": "E1",
        "conserved_regions": [
            {"start": 10, "end": 18, "length": 9, "consensus": "GWGNNTKLT", "mean_identity": 0.97},
            {"start": 40, "end": 45, "length": 6, "consensus": "DSRCIC", "mean_identity": 0.95},
        ],
        "structural_query": {
            "taxon_id": 37124,
            "per_source": {
                "pdb": {
                    "n_hits": 8,
                    "organisms": ["Chikungunya virus", "Chikungunya virus strain S27-African"],
                    "query_used": "epitopes",
                    "note": None,
                },
                "emdb": {
                    "n_hits": 2,
                    "organisms": [],
                    "query_used": '("chikungunya") AND ("envelope" OR "glycoprotein")',
                    "note": None,
                },
            },
            "note": None,
        },
        "structural_reasoning": {
            "available": True,
            "pdb_id": "3N40",
            "chain": "A",
            "structure_kind": "assembly_1",
            "assembly_id": 1,
            "n_assembly_copies": 3,
            "neighbor_cutoff": 10.0,
            "pymol_version": "3.1.0",
            "sasa_settings": {"dot_solvent": 1, "dot_density": 3},
            "rsa_threshold": 0.25,
            "contact_cutoff": 8.0,
            "min_map_identity": 0.7,
            "n_exposed": 5,
            "n_buried": 4,
            "selection": {
                "pdb_id": "3N40",
                "score": 12.0,
                "reasons": [
                    "matches query protein term(s): e1",
                    "surface-antigen signal: envelope",
                ],
                "considered": 8,
            },
        },
        "functional_validation": {
            "residue_level_annotation_available": True,
            "annotation_source": "UniProt+SIFTS+IEDB",
            "uniprot_accessions": ["Q8JUX5"],
            "uniprot_release": "Q8JUX5:2025_03",
            "query_date": "2026-06-13",
            "n_uniprot_features": 14,
            "n_iedb_epitope_spans": 3,
            "coincidences": [
                {"residue": 211, "unp_pos": 211, "accession": "Q8JUX5", "source": "IEDB"}
            ],
            "n_candidate_epitope_residues": 5,
        },
        "stage_reports": [
            {
                "stage": "sequence_conservation",
                "order": 1,
                "markdown": "Aligned 25 per-strain sequences; 120 conserved column(s)...",
                "data": {
                    "available": True,
                    "n_sequences": 25,
                    "n_conserved_columns": 120,
                    "n_conserved_regions": 2,
                    "conservation_threshold": 0.9,
                    "aligner": "mafft",
                    "aligner_version": "v7.526 (2024/Apr/19)",
                },
            },
        ],
    }


def test_full_bundle_every_field_non_empty():
    prov = collect_provenance(_full_bundle())

    assert prov["schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert prov["llm_model"]  # resolve_llm_model → env-or-default, never empty
    # run_id is a named null at collection time (stamped post-run at the run_workflow seam).
    assert "run_id" in prov

    inp = prov["inputs"]
    assert inp["query"] and inp["taxon_id"] and inp["protein"]

    seq = prov["sequence_stage"]
    assert seq["available"] is True
    assert seq["aligner"] == "mafft"
    assert seq["aligner_version"] and seq["aligner_version"] != "unknown"
    assert seq["conservation_threshold"] == 0.9
    assert seq["n_sequences"] == 25
    assert seq["n_conserved_regions"] == 2

    sr_block = prov["structural_retrieval"]
    assert sr_block["available"] is True
    assert sr_block["taxon_id"] == 37124
    assert sr_block["per_source"]["pdb"]["n_hits"] == 8
    assert sr_block["per_source"]["pdb"]["organisms"]
    assert sr_block["per_source"]["emdb"]["query_used"]

    rea = prov["structural_reasoning"]
    assert rea["available"] is True
    assert rea["pdb_id"] == "3N40"
    assert rea["chain"] == "A"
    assert rea["structure_kind"] == "assembly_1"
    assert rea["assembly_id"] == 1
    assert rea["n_assembly_copies"] == 3
    assert rea["neighbor_cutoff"] == 10.0
    assert rea["pymol_version"] == "3.1.0"
    assert rea["sasa_dot_solvent"] == 1
    assert rea["sasa_dot_density"] == 3
    assert rea["rsa_threshold"] == 0.25
    assert rea["contact_cutoff"] == 8.0
    assert rea["n_exposed"] == 5
    assert rea["n_buried"] == 4
    assert rea["ranking_rationale"]  # non-empty list of reasons
    assert rea["n_considered"] == 8

    fv = prov["functional_validation"]
    assert fv["residue_level_annotation_available"] is True
    assert fv["uniprot_accessions"] == ["Q8JUX5"]
    assert fv["uniprot_release"]
    assert fv["sifts_pdb_id"] == "3N40"  # bridged from the structural pdb
    assert fv["query_date"] == "2026-06-13"
    assert fv["n_iedb_epitope_spans"] == 3
    assert fv["n_coincidences"] == 1
    assert fv["n_candidate_epitope_residues"] == 5


def test_records_analyzed_structures_multi():
    """E3-13: when the structural stage analysed the top-N structures, provenance records the
    N analysed ids + per-structure kind (extends, does not break, the single-structure shape)."""
    bundle = _full_bundle()
    bundle["structural_reasoning"].update(
        n_analyzed_structures=3,
        n_corroborated=2,
        analyzed_structures=[
            {"pdb_id": "3N40", "structure_kind": "assembly_1", "available": True},
            {"pdb_id": "2XFB", "structure_kind": "assembly_1", "available": True},
            {
                "pdb_id": "6NK7",
                "structure_kind": "asymmetric_unit",
                "available": False,
                "note": "container error",
            },
        ],
    )
    rea = collect_provenance(bundle)["structural_reasoning"]
    assert rea["n_analyzed_structures"] == 3
    assert rea["n_corroborated"] == 2
    assert [s["pdb_id"] for s in rea["analyzed_structures"]] == ["3N40", "2XFB", "6NK7"]
    assert rea["analyzed_structures"][2]["available"] is False


def test_records_mmcif_assembly_structure_kind():
    """R1: provenance threads the new ``mmcif_assembly`` structure_kind through unchanged
    (a large-assembly run is a real biological assembly, distinct from an AU degrade)."""
    bundle = _full_bundle()
    bundle["structural_reasoning"].update(
        pdb_id="6N1D",
        chain="AS04",
        structure_kind="mmcif_assembly",
        assembly_id=1,
        n_assembly_copies=1,
        neighbor_cutoff=None,
        analyzed_structures=[
            {"pdb_id": "6N1D", "structure_kind": "mmcif_assembly", "available": True}
        ],
        n_analyzed_structures=1,
    )
    rea = collect_provenance(bundle)["structural_reasoning"]
    assert rea["available"] is True
    assert rea["pdb_id"] == "6N1D"
    assert rea["structure_kind"] == "mmcif_assembly"
    assert rea["assembly_id"] == 1
    assert rea["analyzed_structures"][0]["structure_kind"] == "mmcif_assembly"


def test_single_structure_bundle_records_one_analyzed_structure():
    """A pre-E3-13 single-structure bundle (no analyzed_structures key) still yields a
    non-empty one-entry analyzed_structures list derived from the primary pdb."""
    rea = collect_provenance(_full_bundle())["structural_reasoning"]
    assert rea["n_analyzed_structures"] == 1
    assert rea["analyzed_structures"] == [
        {"pdb_id": "3N40", "structure_kind": "assembly_1", "available": True}
    ]


def test_partial_bundle_named_nulls_not_missing_keys():
    """A run where the structural/functional/sequence legs all degraded: every block is
    still present with available=False + a named note, and the keys exist as None (not
    silently absent). No crash."""
    bundle = {
        "query": "obscure virus xyz",
        "structural_note": "No PDB or EMDB structural records were found for 'obscure virus xyz'.",
        "structural_reasoning": {
            "available": False,
            "pdb_id": None,
            "note": "No loadable PDB structure among 0 structural record(s).",
        },
        "stage_reports": [
            {
                "stage": "sequence_conservation",
                "order": 1,
                "markdown": "Sequence conservation unavailable: no usable taxon_id.",
                "data": {"available": False, "note": "no usable taxon_id"},
            }
        ],
    }
    prov = collect_provenance(bundle)

    seq = prov["sequence_stage"]
    assert seq["available"] is False
    assert seq["note"]  # named reason, not missing
    assert seq["aligner"] is None and seq["aligner_version"] is None
    assert "conservation_threshold" in seq and seq["conservation_threshold"] is None

    rea = prov["structural_reasoning"]
    assert rea["available"] is False
    assert rea["note"]
    # Every determinism key is present as a named null, not missing.
    for key in ("pymol_version", "sasa_dot_solvent", "structure_kind", "assembly_id", "n_exposed"):
        assert key in rea and rea[key] is None

    sret = prov["structural_retrieval"]
    assert sret["available"] is False
    assert sret["note"]
    assert sret["per_source"] == {}

    fv = prov["functional_validation"]
    assert fv["available"] is False
    assert fv["residue_level_annotation_available"] is False
    assert "uniprot_accessions" in fv and fv["uniprot_accessions"] == []
    assert "sifts_pdb_id" in fv and fv["sifts_pdb_id"] is None


def test_functional_no_uniprot_xref_named_nulls():
    """Real-path functional stage ran but the chosen structure has NO UniProt xref:
    the UniProt/IEDB/SIFTS fields are explicit named nulls, the candidate counts are real."""
    bundle = {
        "query": "q",
        "structural_reasoning": {"available": True, "pdb_id": "9XYZ", "chain": "A"},
        "functional_validation": {
            "residue_level_annotation_available": False,
            "annotation_source": "none",
            "n_candidate_epitope_residues": 3,
            "n_conserved_regions": 2,
            "coincidences": [],
        },
    }
    fv = collect_provenance(bundle)["functional_validation"]
    assert fv["residue_level_annotation_available"] is False
    assert fv["uniprot_accessions"] == []
    assert fv["uniprot_release"] is None
    assert fv["sifts_pdb_id"] is None  # not bridged: no xref despite a selected structure
    assert fv["query_date"] is None
    assert fv["n_coincidences"] == 0
    assert fv["n_candidate_epitope_residues"] == 3


def test_collect_never_raises_on_garbage():
    for garbage in (None, [], "x", 5, {"stage_reports": "not-a-list"}):
        prov = collect_provenance(garbage)  # type: ignore[arg-type]
        assert prov["schema_version"] == PROVENANCE_SCHEMA_VERSION
        assert prov["llm_model"]
        # All four science blocks always present.
        for block in (
            "sequence_stage",
            "structural_retrieval",
            "structural_reasoning",
            "functional_validation",
        ):
            assert block in prov


# ----------------------- seam threading: review → gate → envelope -----------------------
def test_provenance_threads_review_gate_envelope(tmp_path, monkeypatch):
    """The record collected at review survives gate + envelope into WorkflowResult.provenance."""
    from apecx_integration.composition.steps.design_gate_step import DesignGateStep
    from apecx_integration.composition.steps.envelope_step import EnvelopeStep

    monkeypatch.setattr(
        "apecx_integration.agents.rag_synthesis.synthesize_response",
        lambda q, **k: "# Evidence\n\nBody.",
    )
    review_yml = tmp_path / "review.yml"
    review_yml.write_text("name: review_test\n")
    review = review_mod.EvidenceReviewSynthesisStep.from_config(str(review_yml))

    out = asyncio.run(review.process(_full_bundle()))
    assert out["markdown"]
    prov = out["provenance"]
    assert prov["structural_reasoning"]["pdb_id"] == "3N40"

    gate_yml = tmp_path / "gate.yml"
    gate_yml.write_text("name: gate_test\n")
    gate = DesignGateStep.from_config(str(gate_yml))
    gate_out = asyncio.run(
        gate.process({"review_in": out, "control_in": {"requested_outputs": "evidence_only"}})
    )
    assert gate_out["provenance"]["structural_reasoning"]["pdb_id"] == "3N40"

    env_yml = tmp_path / "env.yml"
    env_yml.write_text("name: env_test\n")
    envelope = EnvelopeStep.from_config(str(env_yml))
    env_out = asyncio.run(envelope.process({"envelope_input": gate_out}))
    wr = env_out["workflow_result"]
    assert wr["status"] == "ok"
    assert wr["provenance"]["structural_reasoning"]["pdb_id"] == "3N40"
    assert wr["provenance"]["sequence_stage"]["aligner_version"]
    assert wr["provenance"]["llm_model"]
