"""LocalMafftAlignStep (EO-53) — real MAFFT alignment (CONTAINER-ONLY) + FAIL-LOUD behavior.

MAFFT is self-provisioning: it runs in the `apecx-mafft` docker image (built on first use, uniform
with the PyMOL container) — there is NO host binary. The FAIL-LOUD tests (missing input, <2 seqs,
Docker absent) pin the anti-mock contract (no silent "mock alignment" fallback). The real-alignment
test is gated on Docker (which builds + runs the container).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep

pytestmark = pytest.mark.integration

_TWO_PROTEINS = (
    ">a\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ\n"
    ">b\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVA\n"
    ">c\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIDVQ\n"
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker unavailable (MAFFT is container-only)"
)


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


def test_no_docker_degrades_loud_not_mock(tmp_path, monkeypatch):
    # The defining test: when the MAFFT image cannot be built (Docker absent / daemon down / build
    # failure), the step RAISES a clear container-only error — it does NOT silently copy input as a
    # "mock alignment". Simulated by making ensure_docker_image_built raise, so it needs no Docker.
    import apecx_integration.composition.steps.local_mafft_align_step as mod

    async def _boom(**kwargs):
        raise RuntimeError("simulated: docker daemon unreachable")

    monkeypatch.setattr(mod, "ensure_docker_image_built", _boom)
    monkeypatch.setenv(
        "APECX_CONSERVED_SITES_NOCACHE", "1"
    )  # force the align path (skip a cache HIT)
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="container-only|Docker|could not be built"):
        asyncio.run(step.process({"fasta_text": _TWO_PROTEINS}))


def test_unwrap_single_key_envelope(tmp_path):
    step = _stage(tmp_path)
    assert step._unwrap({"du": {"fasta_text": "x"}}) == {"fasta_text": "x"}


# --------------------------------------------------------------------------- #
# Real MAFFT alignment
# --------------------------------------------------------------------------- #
@needs_docker
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
