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

CONSERVED-SITES ALIGN CACHE (E3-9, Option B): MAFFT on ~25 long polyprotein sequences is the
~6-minute end-to-end bottleneck. The alignment is deterministic given the same input
sequences + aligner + params, so it is content-addressed and cached under
``~/.cache/apecx_conserved_sites`` (override ``$APECX_CONSERVED_SITES_CACHE``). The KEY is a
sha256 over {aligner, mode, ``--amino``, executable, G24 content-hash of the input FASTA}; the
sequence-hash means a BV-BRC corpus change (different fetched bytes) MISSES and re-aligns — the
fetch step always runs live, so the cache never hides new sequences. The conservation threshold
is NOT in the key (it does not affect the alignment, and the conserve step always re-runs). On a
HIT, MAFFT is skipped and the stored alignment is returned with the live payload's taxon_id/
protein re-applied, so a HIT is byte-identical to a FRESH run (CC-4). A read/write failure or a
corrupt entry degrades to a normal uncached alignment with a warning, never raises (CC-2/G127).
RESIDUAL STALENESS: the MAFFT *version* is NOT in the key (probing it requires running the
binary); a MAFFT upgrade that changes the alignment is not auto-invalidated — force a refresh
with ``$APECX_CONSERVED_SITES_NOCACHE=1`` (see ``_align_cache`` for the full contract).
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobrain.core.step import BaseStep, StepConfig
from nanobrain.library.runtime.container_admission import acquire_container_slot
from nanobrain.library.runtime.docker_image_builder import ensure_docker_image_built
from pydantic import Field

from . import _align_cache

log = logging.getLogger(__name__)

# Self-provisioning MAFFT container (uniform with `_pymol_container`): built on first use by
# `ensure_docker_image_built`, shipped in the wheel via the pyproject `**/_mafft_container/*`
# package-data glob. CONTAINER-ONLY — no host `mafft` binary; the conservation leg degrades loud
# when Docker is unavailable. The image tag PINS the MAFFT version for alignment determinism.
_BUILD_CONTEXT = Path(__file__).resolve().parent / "_mafft_container"
_DOCKERFILE = _BUILD_CONTEXT / "Dockerfile"
_IMAGE_TAG = "apecx-mafft:7.505"  # Debian bookworm MAFFT (see _mafft_container/Dockerfile)


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

        # E3-9 Option B cache: skip MAFFT (the ~6-min bottleneck) when this exact set of fetched
        # sequences + aligner params was aligned before. Key includes the G24 content-hash of the
        # FASTA, so a BV-BRC corpus change MISSES; cache failures degrade loud, never raise (CC-2).
        cache_key = _align_cache.align_cache_key(
            aligner="mafft",
            mode=self._mode,
            amino=self._amino,
            executable=_IMAGE_TAG,  # key on the pinned container image, not a host binary path
            fasta_text=fasta_text,
        )
        if not _align_cache.nocache_enabled():
            cached = _align_cache.read_cached(cache_key)
            if cached is not None:
                self.nb_logger.info(
                    "LocalMafftAlignStep %s: conserved-sites align CACHE HIT (key %s…) — "
                    "skipping MAFFT",
                    self.name,
                    cache_key[:12],
                )
                return {"alignment": self._with_live_context(cached, payload)}

        self.emit_progress(f"aligning {fasta_text.count('>')} sequences (MAFFT, containerized)")
        aligned = await self._run_mafft_container(fasta_text)
        n_seqs = aligned.count(">")
        self.emit_progress(f"alignment complete: {n_seqs} sequences")
        # All aligned records share one length; derive it from the first record.
        alignment_length = _first_record_length(aligned)
        self.nb_logger.info(
            "LocalMafftAlignStep %s: aligned %d sequences (length %d) with %s",
            self.name,
            n_seqs,
            alignment_length,
            _IMAGE_TAG,
        )
        out: dict[str, Any] = {
            "alignment_fasta": aligned,
            "n_sequences": n_seqs,
            "alignment_length": alignment_length,
            "aligner": "mafft",
            # E3-8 provenance: the pinned MAFFT container tag (determinism-relevant — different MAFFT
            # versions can produce different alignments → different conserved sites; the tag pins it).
            "aligner_version": _IMAGE_TAG,
        }
        out = self._with_live_context(out, payload)
        if not _align_cache.nocache_enabled():
            _align_cache.write_cached(cache_key, out)
        return {"alignment": out}

    @staticmethod
    def _with_live_context(alignment: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Return ``alignment`` with the LIVE payload's taxon_id/protein re-applied.

        The expensive, deterministic parts (alignment_fasta/length/aligner/version) come from
        the (possibly cached) alignment; the cheap pass-through identifiers are taken from the
        current payload so a cache HIT is byte-identical to a FRESH run for the same input. The
        ``aligner_version`` is deliberately NOT re-probed: it describes the alignment that was
        actually produced (the cached, older one on a post-upgrade HIT — the residual staleness).

        ``records`` / ``n_fetched`` / ``n_dropped_length_outlier`` also ride through from the
        live payload — they describe the per-strain input set actually fed to this alignment
        (the report's fetched-vs-used disclosure), and like taxon_id/protein they belong to the
        CURRENT input, not the cached alignment, so a cache HIT carries the right strains.
        """
        out = dict(alignment)
        for key in (
            "taxon_id",
            "protein",
            "requested_protein",
            "substituted_protein",
            "records",
            "n_fetched",
            "n_dropped_length_outlier",
        ):
            if key in payload:
                out[key] = payload[key]
            else:
                out.pop(key, None)
        return out

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

    async def _run_mafft_container(self, fasta_text: str) -> str:
        """Align in the SELF-PROVISIONING MAFFT container (no host binary). Build the image if absent,
        then ``docker run mafft … /work/input.fasta`` and capture the alignment from STDOUT.
        CONTAINER-ONLY (uniform with the PyMOL container) — degrades LOUD when Docker is unavailable so
        the conservation leg surfaces a named miss, never a silent skip."""
        import asyncio

        try:
            await ensure_docker_image_built(
                dockerfile_path=str(_DOCKERFILE),
                build_context=str(_BUILD_CONTEXT),
                image_tag=_IMAGE_TAG,
            )
        except Exception as exc:  # noqa: BLE001 — Docker absent / daemon down / build failed → degrade
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': the MAFFT container ({_IMAGE_TAG}) could not be "
                f"built ({type(exc).__name__}: {exc}). MAFFT is container-only — install + start "
                f"Docker. No host-binary fallback by design; the conservation leg degrades without it."
            ) from exc

        with tempfile.TemporaryDirectory(prefix="apecx_mafft_") as tmp:
            workdir = Path(tmp)
            (workdir / "input.fasta").write_text(fasta_text, encoding="utf-8")
            container_name = f"apecx-mafft-{uuid4().hex[:12]}"
            try:
                async with acquire_container_slot():
                    proc = await asyncio.to_thread(self._docker_run_mafft, workdir, container_name)
            except subprocess.TimeoutExpired as exc:
                # Kill the CONTAINER by name (best-effort, off-loop) — the subprocess timeout only
                # SIGKILLs the docker CLI, leaving the container running (--rm removes it only after
                # it stops).
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        subprocess.run,
                        ["docker", "kill", container_name],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                raise ValueError(
                    f"LocalMafftAlignStep '{self.name}': MAFFT (container) timed out after "
                    f"{self._timeout}s"
                ) from exc

        if proc.returncode != 0:
            hint = (
                " (exit 137 = the container was OOM-killed — the 2 GB cap was exceeded by a large "
                "alignment; reduce the sequence set or raise the container memory)"
                if proc.returncode == 137
                else ""
            )
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': MAFFT (container) exited {proc.returncode}.{hint} "
                f"stderr tail: {proc.stderr[-500:]!r}"
            )
        aligned = proc.stdout
        if not aligned.strip() or aligned.count(">") < 2:
            raise ValueError(
                f"LocalMafftAlignStep '{self.name}': MAFFT (container) produced no usable alignment "
                f"(got {aligned.count('>')} records). stderr tail: {proc.stderr[-300:]!r}"
            )
        return aligned

    def _docker_run_mafft(
        self, workdir: Path, container_name: str
    ) -> subprocess.CompletedProcess[str]:
        """Hardened ``docker run`` of MAFFT (network-isolated, cap-dropped, mem/pids-capped, host-uid;
        mirrors the PyMOL container's argv). MAFFT reads /work/input.fasta and writes the MSA to
        STDOUT (captured here) — it writes nothing back to /work. ``--name`` makes the container
        killable-by-name so a timeout can ``docker kill`` it instead of orphaning it."""
        mafft_cmd = ["mafft", self._mode]
        if self._amino:
            mafft_cmd.append("--amino")
        mafft_cmd.append("/work/input.fasta")
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "2048m",
            "--memory-swap",
            "2048m",
            "--pids-limit",
            "256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,source={workdir.resolve()},target=/work",
            "--workdir",
            "/work",
            _IMAGE_TAG,
            *mafft_cmd,
        ]
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=self._timeout, check=False
        )


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
