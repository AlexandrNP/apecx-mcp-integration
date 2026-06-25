"""EF5 — the opt-in degrade-to-MAFFT flag on RheaMuscleAlignStep.

The rhea-unreachable CONDITION is simulated by patching `_drive_rhea_muscle` to raise (so the test is
deterministic regardless of whether a rhea server happens to be up). The DEGRADE BEHAVIOR under test is
REAL — it runs the actual local MAFFT binary (mafft-gated), not a mock. The genuine end-to-end (rhea truly
down) was verified manually: default → raises ValueError; opt-in → degrades to real MAFFT with a note.

Contract:
- ``degrade_to_local_mafft=False`` (DEFAULT, the deliberate "NO silent degradation: raise" design) →
  a rhea-unreachable failure RE-RAISES.
- ``degrade_to_local_mafft=True`` (OPT-IN) → degrades LOUD to local MAFFT (same alignment shape) +
  a ``degrade_note`` + ``degraded_from='muscle'``. Never silent.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from apecx_integration.composition.steps.rhea_muscle_align_step import RheaMuscleAlignStep

needs_mafft = pytest.mark.skipif(
    shutil.which("mafft") is None, reason="needs the MAFFT binary for the degrade fallback"
)
_FASTA = ">a\nMKTAYIAKQRQISFVK\n>b\nMKTAYIAKQRQISFVK\n>c\nMKTAYIAKQRQXSFVK\n"


def _step(degrade: bool) -> RheaMuscleAlignStep:
    cfg: dict = {"name": "rhea_align_test", "timeout_seconds": 10}
    if degrade:
        cfg["degrade_to_local_mafft"] = True
    s = RheaMuscleAlignStep.from_config(cfg)

    async def _boom(_fasta_text: str) -> dict:
        # the exact shape of the real rhea-unreachable failure (rhea_muscle_align_step.py:157)
        raise ValueError(
            "simulated: rhea subworkflow produced no 'workflow_output' (rhea unreachable)"
        )

    s._drive_rhea_muscle = _boom  # type: ignore[method-assign]
    return s


@needs_mafft
def test_fail_closed_default_raises_on_rhea_unreachable():
    """Default preserves the deliberate fail-closed design — rhea-down RE-RAISES (no silent degrade)."""
    with pytest.raises(ValueError):
        asyncio.run(_step(degrade=False).process({"fasta_text": _FASTA}))


@needs_mafft
def test_optin_degrades_loud_to_mafft_on_rhea_unreachable():
    """Opt-in degrades LOUD to real MAFFT — same shape + a degrade_note (EF5)."""
    out = asyncio.run(_step(degrade=True).process({"fasta_text": _FASTA}))
    al = out["alignment"]
    assert al["aligner"] == "mafft"
    assert al["degraded_from"] == "muscle"
    assert "MAFFT" in al["degrade_note"]
    assert al["alignment_fasta"].count(">") >= 2  # real alignment of the 3 input sequences
