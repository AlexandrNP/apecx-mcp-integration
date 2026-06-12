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


def test_length_filter_drops_partials(tmp_path):
    # Network-free: exercise the partial-record filter directly.
    step = _stage(tmp_path, min_length_fraction="0.8")
    records = [
        {"id": "full1", "sequence": "M" * 100, "product": "", "genome_name": ""},
        {"id": "full2", "sequence": "M" * 95, "product": "", "genome_name": ""},
        {"id": "partial", "sequence": "M" * 40, "product": "", "genome_name": ""},  # 40% → dropped
    ]
    kept = step._apply_length_filter(records)
    assert {r["id"] for r in kept} == {"full1", "full2"}


def test_length_filter_disabled_by_default(tmp_path):
    step = _stage(tmp_path)  # min_length_fraction default 0.0
    records = [
        {"id": "a", "sequence": "M" * 100, "product": "", "genome_name": ""},
        {"id": "b", "sequence": "M" * 10, "product": "", "genome_name": ""},
    ]
    assert len(step._apply_length_filter(records)) == 2  # nothing dropped


def test_length_filter_too_aggressive_fails_loud(tmp_path):
    step = _stage(tmp_path, min_length_fraction="0.95")
    records = [
        {"id": "full", "sequence": "M" * 100, "product": "", "genome_name": ""},
        {"id": "p1", "sequence": "M" * 50, "product": "", "genome_name": ""},
        {"id": "p2", "sequence": "M" * 60, "product": "", "genome_name": ""},
    ]
    with pytest.raises(ValueError, match="length filter"):
        step._apply_length_filter(records)


@needs_bvbrc
def test_unknown_protein_fails_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="no .* protein features|at least 2"):
        asyncio.run(
            step.process(
                {"taxon_id": _CHIKV_TAXON, "protein": "zzznotaprotein", "feature_type": "CDS"}
            )
        )
