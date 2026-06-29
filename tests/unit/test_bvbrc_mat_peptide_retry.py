"""Mature-peptide retry (2026-06-29 polyprotein-conservation finding).

A mature protein of a POLYPROTEIN virus (alphavirus capsid/E1/E2/6K, flavivirus envelope, ...) is
annotated in BV-BRC as a ``mat_peptide`` feature, NOT a CDS (the CDS is the whole polyprotein), so a
CDS fetch for the mature protein finds <2 sequences. BvbrcProteinFastaStep must retry the SAME protein
as ``mat_peptide`` BEFORE substituting a DIFFERENT product — giving real per-mature-protein
conservation. Real-data e2e behind this: EEEV "capsid protein" → 0 CDS but ~1200 mat_peptide.

``_fetch`` is mocked (no network) so the retry control-flow is pinned deterministically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep


def _stage(tmp_path: Path) -> BvbrcProteinFastaStep:
    p = tmp_path / "fasta.yml"
    p.write_text("name: fasta_test\n")
    return BvbrcProteinFastaStep.from_config(str(p))


def _recs(n: int) -> list[dict]:
    return [
        {"id": f"f{i}", "product": "capsid protein", "genome_name": "g", "sequence": "MKAAVT"}
        for i in range(n)
    ]


def test_mat_peptide_retry_recovers_same_protein_before_substituting(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_fetch(taxon_id, protein, feature_type):
        calls.append((protein, feature_type))
        if feature_type == "mat_peptide" and protein == "capsid protein":
            return _recs(3), 3, 0
        return [], 0, 0  # CDS: only the polyprotein → <2 for the mature protein

    monkeypatch.setattr(step, "_fetch", fake_fetch)

    def _no_substitute(*a, **k):
        raise AssertionError("must NOT substitute a different product when mat_peptide succeeds")

    monkeypatch.setattr(step, "_query_available_proteins", _no_substitute)
    out = asyncio.run(step.process({"taxon_id": 11021, "protein": "capsid protein"}))
    pf = out["protein_fasta"]
    assert pf["feature_type"] == "mat_peptide"  # records came from the mat_peptide retry
    assert pf["substituted_protein"] is None  # SAME protein, not a substitution
    assert pf["requested_protein"] == "capsid protein"
    assert len(pf["records"]) >= 2
    assert ("capsid protein", "mat_peptide") in calls  # the retry was issued for the SAME protein


def test_mat_peptide_retry_falls_through_to_substitute_when_also_empty(tmp_path, monkeypatch):
    # When the SAME protein has <2 in BOTH CDS and mat_peptide, the existing substitute fallback still
    # runs (no regression to the too-few-sequences path).
    step = _stage(tmp_path)

    def fake_fetch(taxon_id, protein, feature_type):
        if protein == "envelope glycoprotein E2":
            return _recs(4), 4, 0  # the substitute product DOES have sequences (CDS)
        return [], 0, 0  # requested protein: nothing in CDS or mat_peptide

    monkeypatch.setattr(step, "_fetch", fake_fetch)
    monkeypatch.setattr(
        step, "_query_available_proteins", lambda *a, **k: [("envelope glycoprotein E2", 7)]
    )
    out = asyncio.run(step.process({"taxon_id": 11021, "protein": "nonexistent mature peptide"}))
    pf = out["protein_fasta"]
    assert pf["substituted_protein"] == "envelope glycoprotein E2"
    assert len(pf["records"]) >= 2
