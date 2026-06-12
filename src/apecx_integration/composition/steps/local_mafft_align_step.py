"""LocalMafftAlignStep — multiple-sequence alignment via a local MAFFT binary (EO-53).

The lightweight, dependency-light alignment path for the conserved-sites workflow: it shells
out to a real MAFFT executable (a standard, arm64-native MSA aligner). This is one of the
"multiple legit ways" to align — the heavier production path is the Rhea/Galaxy tool dispatch
(``rhea_muscle_alignment`` workflow), substitutable per design §8. Choosing MAFFT here also
demonstrates the pipeline is NOT confined to MUSCLE.

Real subprocess, NO mocks, NO silent degradation: if the MAFFT binary is absent, or it exits
non-zero, or it emits no alignment, the step FAILS LOUD. This is the deliberate opposite of
the abandoned SequenceAnalysisStep, which fabricated a "mock alignment" by copying its input
when MUSCLE was unavailable.

Input  (after trigger-envelope unwrap): ``{"fasta_text": "<unaligned FASTA>", ...}``.
Output: ``{"alignment": {alignment_fasta, n_sequences, alignment_length, aligner, ...}}``.
Any ``taxon_id`` / ``protein`` present on the input are passed through for downstream context.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)


class LocalMafftAlignStepConfig(StepConfig):
    """Config for local MAFFT alignment. A StepConfig subclass — no ``extra='forbid'``."""

    mafft_executable: str = Field(default="mafft")
    amino: bool = Field(default=True, description="Pass --amino (protein sequences).")
    mode: str = Field(default="--auto", description="MAFFT strategy flag (e.g. --auto).")
    timeout_seconds: float = Field(default=300.0, gt=0)


class LocalMafftAlignStep(BaseStep):
    """Align an unaligned FASTA with a local MAFFT binary; emit the aligned FASTA."""

    COMPONENT_TYPE = "local_mafft_align_step"

    @classmethod
    def _get_config_class(cls):
        return LocalMafftAlignStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._mafft: str = getattr(config, "mafft_executable", "mafft")
        self._amino: bool = bool(getattr(config, "amino", True))
        self._mode: str = getattr(config, "mode", "--auto")
        self._timeout: float = float(getattr(config, "timeout_seconds", 300.0))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        import asyncio

        payload = self._unwrap(input_data)
        fasta_text = payload.get("fasta_text")
        if not isinstance(fasta_text, str) or not fasta_text.strip():
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': input must carry a non-empty 'fasta_text' "
                f"string; got {type(fasta_text).__name__}"
            )
        if fasta_text.count(">") < 2:
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': need ≥2 sequences to align; the input FASTA "
                f"has {fasta_text.count('>')}."
            )

        aligned = await asyncio.to_thread(self._run_mafft, fasta_text)
        n_seqs = aligned.count(">")
        # All aligned records share one length; derive it from the first record.
        alignment_length = _first_record_length(aligned)
        self.nb_logger.info(
            "LocalMafftAlignStep %s: aligned %d sequences (length %d) with %s",
            self.name,
            n_seqs,
            alignment_length,
            self._mafft,
        )
        out: dict[str, Any] = {
            "alignment_fasta": aligned,
            "n_sequences": n_seqs,
            "alignment_length": alignment_length,
            "aligner": "mafft",
        }
        # Pass through identifying context for the downstream report.
        for key in ("taxon_id", "protein"):
            if key in payload:
                out[key] = payload[key]
        return {"alignment": out}

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if "fasta_text" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    def _run_mafft(self, fasta_text: str) -> str:
        if shutil.which(self._mafft) is None:
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': MAFFT executable {self._mafft!r} not found "
                f"on PATH. Install it (e.g. `brew install mafft` / `conda install -c bioconda "
                f"mafft`), or use the Rhea alignment path. (No mock fallback — alignment is real.)"
            )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as fh:
            fh.write(fasta_text)
            in_path = fh.name
        try:
            cmd = [self._mafft, self._mode]
            if self._amino:
                cmd.append("--amino")
            cmd.append(in_path)
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self._timeout, check=False
            )
            if proc.returncode != 0:
                raise ValueError(
                    f"LocalMafftAlignStep '{self.name}': MAFFT exited {proc.returncode}. "
                    f"stderr tail: {proc.stderr[-500:]!r}"
                )
            aligned = proc.stdout
            if not aligned.strip() or aligned.count(">") < 2:
                raise ValueError(
                    f"LocalMafftAlignStep '{self.name}': MAFFT produced no usable alignment "
                    f"(got {aligned.count('>')} records). stderr tail: {proc.stderr[-300:]!r}"
                )
            return aligned
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': MAFFT timed out after {self._timeout}s"
            ) from exc
        finally:
            if os.path.exists(in_path):
                os.unlink(in_path)


def _first_record_length(aligned_fasta: str) -> int:
    """Length of the first aligned record (all records share it in an MSA)."""
    seq: list[str] = []
    started = False
    for line in aligned_fasta.splitlines():
        if line.startswith(">"):
            if started:
                break
            started = True
        elif started:
            seq.append(line.strip())
    return len("".join(seq))


__all__ = ["LocalMafftAlignStep", "LocalMafftAlignStepConfig"]
