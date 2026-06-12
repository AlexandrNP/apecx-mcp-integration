"""LocalMafftAlignStep (EO-53) — real MAFFT alignment + FAIL-LOUD behavior.

The FAIL-LOUD tests (missing input, <2 seqs, MAFFT binary absent) need no MAFFT and run
unconditionally — they pin the anti-mock contract (no silent "mock alignment" fallback). The
real-alignment test is gated on a MAFFT binary being installed.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep

pytestmark = pytest.mark.integration

_TWO_PROTEINS = (
    ">a\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ\n"
    ">b\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVA\n"
    ">c\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIDVQ\n"
)

needs_mafft = pytest.mark.skipif(shutil.which("mafft") is None, reason="MAFFT not installed")


def _stage(tmp_path: Path, **cfg) -> LocalMafftAlignStep:
    p = tmp_path / "mafft.yml"
    lines = ["name: mafft_test"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return LocalMafftAlignStep.from_config(str(p))


# --------------------------------------------------------------------------- #
# FAIL-LOUD — no MAFFT required (the anti-mock-fallback contract)
# --------------------------------------------------------------------------- #
def test_missing_fasta_text_is_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="fasta_text"):
        asyncio.run(step.process({"records": []}))


def test_too_few_sequences_is_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="≥2|sequences"):
        asyncio.run(step.process({"fasta_text": ">only\nMKTAY\n"}))


def test_absent_binary_fails_loud_not_mock(tmp_path):
    # The defining test: when the aligner binary is missing, the step RAISES (with an install
    # hint) — it does NOT silently copy input as a "mock alignment" like the abandoned step.
    step = _stage(tmp_path, mafft_executable="definitely_not_a_real_binary_xyz")
    with pytest.raises(ValueError, match="not found|No mock"):
        asyncio.run(step.process({"fasta_text": _TWO_PROTEINS}))


def test_unwrap_single_key_envelope(tmp_path):
    step = _stage(tmp_path)
    assert step._unwrap({"du": {"fasta_text": "x"}}) == {"fasta_text": "x"}


# --------------------------------------------------------------------------- #
# Real MAFFT alignment
# --------------------------------------------------------------------------- #
@needs_mafft
def test_real_mafft_alignment(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process({"fasta_text": _TWO_PROTEINS, "taxon_id": 11021, "protein": "X"})
    )
    aln = out["alignment"]
    assert aln["aligner"] == "mafft"
    assert aln["n_sequences"] == 3
    assert aln["alignment_length"] >= 30
    # Real alignment: every record is the same (aligned) length.
    from apecx_integration.composition.steps.conservation_score_step import _parse_fasta

    seqs = _parse_fasta(aln["alignment_fasta"])
    assert len({len(s) for _, s in seqs}) == 1, "aligned records must share one length"
    # Context passed through for the downstream report.
    assert aln["taxon_id"] == 11021
    assert aln["protein"] == "X"
