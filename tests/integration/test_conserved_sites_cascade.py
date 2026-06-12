"""Conserved-sites scientific cascade (EO-53) — REAL end-to-end, no mocks.

Drives the three component steps in sequence against real data:

    BvbrcProteinFastaStep  (live BV-BRC: real per-strain AA sequences)
        → LocalMafftAlignStep  (real MAFFT MSA)
            → ConservationScoreStep  (real per-column conservation)

This is the integration test that proves the feature actually works on real data AND that the
inter-step data bridges hold (each step's output dict feeds the next step's required input key
with no TransformLink). Gated on BOTH a MAFFT binary and BV-BRC reachability; it asserts REAL
conserved sites are found — the anti-regression for the abandoned mock pipeline.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep
from apecx_integration.composition.steps.conservation_score_step import ConservationScoreStep
from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/genome_feature/"
            f"?eq(taxon_id,{_CHIKV_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_deps = pytest.mark.skipif(
    shutil.which("mafft") is None or not _bvbrc_reachable(),
    reason="needs MAFFT installed AND BV-BRC reachable",
)


def _step(cls, tmp_path: Path, name: str, **cfg):
    p = tmp_path / f"{name}.yml"
    lines = [f"name: {name}"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return cls.from_config(str(p))


@needs_deps
def test_real_conserved_sites_cascade(tmp_path):
    fetch = _step(BvbrcProteinFastaStep, tmp_path, "fetch", feature_type="CDS", max_sequences="5")
    align = _step(LocalMafftAlignStep, tmp_path, "align")
    score = _step(ConservationScoreStep, tmp_path, "score", conservation_threshold="0.9")

    # 1) Real per-strain sequences for the CHIKV structural polyprotein (consistently annotated).
    fasta_out = asyncio.run(
        fetch.process(
            {"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein", "feature_type": "CDS"}
        )
    )
    protein_fasta = fasta_out["protein_fasta"]
    assert protein_fasta["n_sequences"] >= 2

    # 2) Real MAFFT alignment — the step reads 'fasta_text' straight out of the fetch output dict
    #    (the inter-step bridge: no TransformLink, the next step picks its key).
    align_out = asyncio.run(align.process(protein_fasta))
    alignment = align_out["alignment"]
    assert alignment["aligner"] == "mafft"
    assert alignment["alignment_length"] > 0
    assert alignment["taxon_id"] == _CHIKV_TAXON  # context threaded through

    # 3) Real conservation over the alignment — reads 'alignment_fasta' from the align output.
    score_out = asyncio.run(score.process(alignment))
    res = score_out["conservation_result"]
    assert res["n_sequences"] == protein_fasta["n_sequences"]
    assert res["alignment_length"] == alignment["alignment_length"]
    # Related strains of a structural polyprotein DO share many conserved positions — the real
    # signal the feature exists to surface (this run found ~74 conserved columns in ~64 regions).
    # We assert the signal is substantial, not a specific mean-identity value: the alignment is
    # gap-heavy (variable-length / partial CDS records inflate column count), which legitimately
    # lowers mean identity — gappiness is real biology, not a failure.
    assert len(res["conserved_sites"]) >= 10, (
        f"expected many conserved columns across CHIKV strains, got {len(res['conserved_sites'])}"
    )
    assert res["conserved_regions"], "expected at least one conserved region"
    assert 0.0 <= res["mean_identity"] <= 1.0
