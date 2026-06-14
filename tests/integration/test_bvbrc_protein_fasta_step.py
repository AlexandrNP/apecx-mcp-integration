"""BvbrcProteinFastaStep (EO-51) — real BV-BRC data-API retrieval.

The live test hits the real BV-BRC data API (no mocks) and asserts REAL amino-acid
sequences come back — this is the anti-regression for the abandoned SequenceAnalysisStep,
which faked sequences. It auto-skips when the API is unreachable. The validation tests are
network-free (they FAIL-LOUD before any request).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124  # Chikungunya virus


def _stage(tmp_path: Path, **cfg) -> BvbrcProteinFastaStep:
    p = tmp_path / "bvbrc_fasta.yml"
    lines = ["name: bvbrc_fasta_test"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return BvbrcProteinFastaStep.from_config(str(p))


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


needs_bvbrc = pytest.mark.skipif(not _bvbrc_reachable(), reason="BV-BRC data API not reachable")


# --------------------------------------------------------------------------- #
# Validation — FAIL-LOUD before any network call (network-free)
# --------------------------------------------------------------------------- #
def test_missing_taxon_id_is_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="taxon_id"):
        asyncio.run(step.process({"protein": "E1"}))


def test_non_numeric_taxon_id_is_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="taxon_id"):
        asyncio.run(step.process({"taxon_id": "not-a-number", "protein": "E1"}))


def test_blank_protein_is_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="protein"):
        asyncio.run(step.process({"taxon_id": _CHIKV_TAXON, "protein": "   "}))


def test_unwrap_single_key_trigger_envelope(tmp_path):
    step = _stage(tmp_path)
    inner = step._unwrap({"some_du": {"taxon_id": 1, "protein": "x"}})
    assert inner == {"taxon_id": 1, "protein": "x"}
    flat = step._unwrap({"taxon_id": 2, "protein": "y"})
    assert flat == {"taxon_id": 2, "protein": "y"}


# --------------------------------------------------------------------------- #
# Real BV-BRC retrieval — actual AA sequences, no mocks
# --------------------------------------------------------------------------- #
@needs_bvbrc
def test_fetches_real_chikv_e1_sequences(tmp_path):
    step = _stage(tmp_path, feature_type="mat_peptide", max_sequences="8")
    out = asyncio.run(
        step.process({"taxon_id": _CHIKV_TAXON, "protein": "E1", "feature_type": "mat_peptide"})
    )
    bundle = out["protein_fasta"]
    assert bundle["n_sequences"] >= 2, bundle
    assert bundle["taxon_id"] == _CHIKV_TAXON

    # Every record carries a REAL amino-acid sequence (alphabetic, non-trivial length) —
    # NOT the "ATCGATCG"*20 nucleotide placeholder the abandoned step produced.
    for rec in bundle["records"]:
        seq = rec["sequence"]
        assert isinstance(seq, str) and len(seq) > 50, rec
        assert seq.isalpha(), f"sequence has non-letter chars: {seq[:40]!r}"
        # AA, not DNA: a real protein uses the wider amino-acid alphabet, not just ACGT.
        assert set(seq.upper()) - set("ACGT"), f"looks like DNA, not protein: {seq[:40]!r}"

    # The FASTA text is well-formed and feeds the alignment subworkflow.
    assert bundle["fasta_text"].startswith(">")
    assert bundle["fasta_text"].count(">") == bundle["n_sequences"]


def _seq(n: int) -> str:
    return "M" * n


def test_length_cluster_keeps_dominant_band_drops_outlier(tmp_path):
    # The dengue-envelope shape: ~50 sequences clustered at ~495aa + a single 1180aa polyprotein
    # outlier. The former 'fraction of the longest' filter (0.8 * 1180 = 944) left only the
    # outlier → <2 → MAFFT failed. Cluster selection must keep the ~495 band, drop the outlier.
    import random

    rng = random.Random(0)
    records = [
        {
            "id": f"env{i}",
            "sequence": _seq(495 + rng.randint(-8, 8)),
            "product": "",
            "genome_name": "",
        }
        for i in range(50)
    ]
    records.append({"id": "polyprotein", "sequence": _seq(1180), "product": "", "genome_name": ""})
    kept = step_cluster(tmp_path)._select_length_cluster(records)
    assert len(kept) == 50  # the whole ~495 cluster
    assert "polyprotein" not in {r["id"] for r in kept}  # the 1180aa outlier is dropped
    assert all(480 <= len(r["sequence"]) <= 510 for r in kept)


def test_length_cluster_drops_short_fragments(tmp_path):
    # Short partial-genome fragments form their own sparse band and are dropped in favor of the
    # full-length cohort.
    records = [
        {"id": "full1", "sequence": _seq(495), "product": "", "genome_name": ""},
        {"id": "full2", "sequence": _seq(498), "product": "", "genome_name": ""},
        {"id": "full3", "sequence": _seq(492), "product": "", "genome_name": ""},
        {"id": "frag", "sequence": _seq(120), "product": "", "genome_name": ""},  # outlier band
    ]
    kept = step_cluster(tmp_path)._select_length_cluster(records)
    assert {r["id"] for r in kept} == {"full1", "full2", "full3"}


def test_length_cluster_genuinely_too_few_fails_loud(tmp_path):
    # Three records, all length-disparate (no band holds ≥2) → genuine named degrade, FAIL-LOUD.
    records = [
        {"id": "a", "sequence": _seq(300), "product": "", "genome_name": ""},
        {"id": "b", "sequence": _seq(600), "product": "", "genome_name": ""},
        {"id": "c", "sequence": _seq(1180), "product": "", "genome_name": ""},
    ]
    with pytest.raises(ValueError, match="no coherent length band"):
        step_cluster(tmp_path)._select_length_cluster(records)


def step_cluster(tmp_path) -> BvbrcProteinFastaStep:
    return _stage(tmp_path, length_cluster_tolerance="0.2")


@needs_bvbrc
def test_unknown_protein_fails_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="no .* protein features|at least 2"):
        asyncio.run(
            step.process(
                {"taxon_id": _CHIKV_TAXON, "protein": "zzznotaprotein", "feature_type": "CDS"}
            )
        )


def test_product_word_boundary_rejects_substring_wrong_protein():
    """Regression: the BV-BRC substring query eq(product,*structural*) matches
    'nonstructural polyprotein' too; the word-boundary filter must reject it (the silent
    wrong-protein bug that made CHIKV align the nonstructural polyprotein)."""
    from apecx_integration.composition.steps.bvbrc_protein_fasta_step import (
        _product_matches_word_boundary as m,
    )

    assert m("structural polyprotein", "structural polyprotein") is True
    assert m("nonstructural polyprotein", "structural polyprotein") is False
    assert m("non-structural polyprotein P1234, fragment", "structural") is False  # hyphenated
    assert m("Non-structural protein 1", "structural") is False  # capitalized hyphen
    assert m("prM-E polyprotein", "E") is True  # legit hyphen compound still matches
    assert m("envelope glycoprotein E", "envelope") is True
    assert m("E1 envelope glycoprotein", "E1") is True
    assert m("", "envelope") is False
