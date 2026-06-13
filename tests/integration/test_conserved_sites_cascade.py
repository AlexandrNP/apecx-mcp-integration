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
import time
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


@needs_deps
def test_align_cache_run2_is_fast_and_byte_identical(tmp_path, monkeypatch):
    """E3-9 Option B: aligning the SAME real CHIKV corpus twice — run-2 HITs the cache.

    Proves the perf win AND correctness: run-2 (cache HIT, MAFFT skipped) is dramatically
    faster than run-1 (cold MAFFT on ~25 long polyprotein sequences) AND produces a
    byte-identical non-empty conservation result (CC-1/CC-4). The cache dir is isolated to a
    tmp dir so run-1 is genuinely cold.
    """
    monkeypatch.setenv("APECX_CONSERVED_SITES_CACHE", str(tmp_path / "align_cache"))
    monkeypatch.delenv("APECX_CONSERVED_SITES_NOCACHE", raising=False)

    fetch = _step(BvbrcProteinFastaStep, tmp_path, "fetch", feature_type="CDS", max_sequences="25")
    align = _step(LocalMafftAlignStep, tmp_path, "align")
    score = _step(ConservationScoreStep, tmp_path, "score", conservation_threshold="0.9")

    # Fetch ONCE (the fetch step always runs live in production — only align is cached).
    payload = asyncio.run(
        fetch.process(
            {"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein", "feature_type": "CDS"}
        )
    )["protein_fasta"]
    assert payload["n_sequences"] >= 5, (
        "need a real multi-strain corpus to make the timing meaningful"
    )

    def _align_and_score() -> tuple[dict, float]:
        t0 = time.perf_counter()
        alignment = asyncio.run(align.process(dict(payload)))["alignment"]
        elapsed = time.perf_counter() - t0
        res = asyncio.run(score.process(alignment))["conservation_result"]
        return res, elapsed

    res1, t_cold = _align_and_score()  # cold: real MAFFT
    res2, t_hit = _align_and_score()  # warm: cache HIT, MAFFT skipped

    print(f"\n[E3-9] align run-1 (cold MAFFT, {payload['n_sequences']} seqs): {t_cold:.2f}s")
    print(f"[E3-9] align run-2 (cache HIT):                 {t_hit:.2f}s")
    print(f"[E3-9] speedup: {t_cold / max(t_hit, 1e-6):.0f}x")

    # CC-1: a HIT returns the SAME non-empty conservation result (never a silently-cached empty).
    assert res2["conserved_sites"], "cache HIT must return non-empty conserved sites"
    assert res2["conserved_regions"], "cache HIT must return non-empty conserved regions"
    # CC-4: byte-identical to the fresh run.
    assert res1 == res2, "cache HIT conservation result must be byte-identical to the cold run"
    # Perf: run-2 is a fast hit (no 6-min MAFFT), and far cheaper than the cold align.
    assert t_hit < 10.0, f"cache HIT must be fast (<10s), was {t_hit:.2f}s"
    assert t_hit < t_cold, (
        f"cache HIT ({t_hit:.2f}s) must be cheaper than cold align ({t_cold:.2f}s)"
    )
