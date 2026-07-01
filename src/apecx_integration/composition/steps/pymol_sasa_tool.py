"""PyMOL-SASA as a self-provisioning nanobrain tool.

A ``ToolBackendAdapter`` (``BACKEND_NAME="pymol"``) that AUTO-BUILDS its Docker image on first use
(reusing nanobrain's ``ensure_docker_image_built``) and runs the headless PyMOL SASA job one-shot per
structure (reusing nanobrain's ``acquire_container_slot``). This is what makes PyMOL a first-class,
self-provisioning nanobrain tool: no ``apecx-setup pymol`` install step — the image builds itself when
a workflow first needs it, which RESOLVES C6 (SASA "unavailable") by construction.

Two surfaces:
- ``invoke(utd, inputs)`` — the framework dispatch contract (for ``ToolExecutionStep`` + the registry).
- ``run_sasa(...)`` — the direct per-structure call ``structural_reasoning_step`` uses inside its
  multi-structure loop (avoids building a UTD per structure; the tool is still registered + invoke-able).

The PyMOL job script + the hardened ``docker run`` shape are apecx-owned (bio-specific); the generic
auto-build + concurrency-slot live in nanobrain so any containerized tool reuses them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from nanobrain.core.component_base import ComponentConfigurationError
from nanobrain.core.unified_tool_descriptor import UnifiedToolDescriptor
from nanobrain.library.runtime.container_admission import acquire_container_slot
from nanobrain.library.runtime.docker_image_builder import (
    ensure_docker_image_built,
    image_digest,
)
from nanobrain.library.steps.tool_execution_step import ToolBackendAdapter, ToolBackendRegistry

# PyMOL container artifacts (apecx-owned), packaged INSIDE the wheel so they resolve in EVERY
# install mode (editable / uv tool / pip wheel): the SASA helper sits next to this module, and the
# job script + Dockerfile live in the _pymol_container/ build context next to it (shipped via the
# pyproject ``**/_pymol_container/*`` package-data glob — a repo-root path does not survive a
# non-editable install).
_SASA_HELPER = Path(__file__).resolve().parent / "_pymol_sasa.py"
_BUILD_CONTEXT = Path(__file__).resolve().parent / "_pymol_container"
_JOB_SCRIPT = _BUILD_CONTEXT / "_pymol_job.py"
_DOCKERFILE = _BUILD_CONTEXT / "Dockerfile"
_DEFAULT_IMAGE = "apecx-pymol:3.1.0"
_KIND_ASSEMBLY = "assembly_1"


class PyMOLToolBackendAdapter(ToolBackendAdapter):
    """Self-provisioning PyMOL-SASA tool. Build the image if absent, then run the one-shot job."""

    BACKEND_NAME = "pymol"

    def __init__(
        self,
        *,
        image_tag: str = _DEFAULT_IMAGE,
        dockerfile_path: str = str(_DOCKERFILE),
        build_context: str = str(_BUILD_CONTEXT),
        job_script: Path = _JOB_SCRIPT,
        sasa_helper: Path = _SASA_HELPER,
    ) -> None:
        self._image_tag = image_tag
        self._dockerfile_path = dockerfile_path
        self._build_context = build_context
        self._job_script = job_script
        self._sasa_helper = sasa_helper
        self._digest: str | None = None

    @classmethod
    def register(cls, **kwargs: Any) -> PyMOLToolBackendAdapter:
        """Build the adapter and register it with the process-global ToolBackendRegistry.

        Returns the already-registered adapter on a repeated call (safe startup idempotency). The
        'pymol' backend is process-wide single-image, so FAIL-FAST if a caller asks for a DIFFERENT
        image_tag than the one already registered rather than silently sharing the first image."""
        requested = kwargs.get("image_tag", _DEFAULT_IMAGE)
        try:
            existing = ToolBackendRegistry.get(cls.BACKEND_NAME)
            if isinstance(existing, cls):
                if requested != existing._image_tag:
                    raise ComponentConfigurationError(
                        f"FAIL-FAST: the 'pymol' tool is already registered with image "
                        f"{existing._image_tag!r}; cannot re-register with {requested!r}."
                    )
                return existing
        except KeyError:
            pass
        adapter = cls(**kwargs)
        ToolBackendRegistry.register(adapter)
        return adapter

    async def ensure_image(self, *, on_progress: Any = None) -> None:
        """Auto-build the PyMOL image if absent (build-locked, off the event loop). FAIL-LOUD
        (DockerImageBuildError) on docker-absent / daemon-down / build-failure."""
        self._verify_artifacts_present()
        await ensure_docker_image_built(
            dockerfile_path=self._dockerfile_path,
            build_context=self._build_context,
            image_tag=self._image_tag,
            on_progress=on_progress,
        )
        if self._digest is None:
            self._digest = await image_digest(self._image_tag)

    def _verify_artifacts_present(self) -> None:
        """FAIL-LOUD with a reinstall hint when the packaged PyMOL container artifacts are missing
        (#1, 2026-07-01). A stale/incomplete install — an MCP server process still running a pre-fix
        build (the reported ``docker/pymol/_pymol_job.py`` FileNotFoundError), or a wheel that didn't
        ship the ``_pymol_container/**`` package-data — would otherwise surface as a BARE
        FileNotFoundError from ``shutil.copy2`` deep inside the run, with no hint that the fix is a
        reinstall + server restart."""
        missing = [
            str(p)
            for p in (
                Path(self._dockerfile_path),
                Path(self._job_script),
                Path(self._sasa_helper),
            )
            if not p.is_file()
        ]
        if missing:
            raise RuntimeError(
                "PyMOL container artifacts not found: "
                + ", ".join(missing)
                + " — the installed apecx build is stale or incomplete (the `_pymol_container/**` "
                "package-data did not ship, or a pre-fix MCP server process is still running). Fix: "
                "reinstall + restart the server — `uv tool install --reinstall apecx-mcp-integration` "
                "(or `pip install -e .` in a dev checkout), then restart apecx-mcp."
            )

    async def ensure_established(self, *, on_progress: Any = None) -> None:
        """Establish PyMOL from its docker source (build the image if absent) — the
        ``Establishable`` hook the find-and-establish seam calls so PyMOL is provisioned
        THROUGH the seam, not around it. Delegates to ``ensure_image`` (no new build logic)."""
        await self.ensure_image(on_progress=on_progress)

    def _docker_argv(self, workdir: Path, memory_mb: int) -> list[str]:
        """Hardened ``docker run`` argv (moved verbatim from structural_reasoning_step): network-isolated,
        cap-dropped, memory/pids-capped, host-uid so the written result.json is host-owned; /work is
        read-write (the job writes result.json back)."""
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            f"{memory_mb}m",
            "--memory-swap",
            f"{memory_mb}m",
            "--pids-limit",
            "256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,source={workdir.resolve()},target=/work",
            "--workdir",
            "/work",
            self._image_tag,
            "python",
            "/work/_pymol_job.py",
            "/work/job.json",
            "/work/result.json",
        ]

    async def run_sasa(
        self,
        *,
        pdb_id: str,
        structure_path: Path,
        kind: str,
        regions: list[dict[str, Any]],
        rsa_threshold: float,
        min_map_identity: float,
        contact_cutoff: float,
        memory_mb: int,
        timeout: float,
        artifacts_dir: Path,
        requested_chain: str | None = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        """Ensure the image is built, then run the headless PyMOL SASA job one-shot and return its
        result dict. Raises DockerImageBuildError (image/docker unavailable) or RuntimeError (container
        error / timeout / missing result) — the caller owns the G127 degrade-loud policy."""
        await self.ensure_image(on_progress=on_progress)
        ext = "pdb1" if kind == _KIND_ASSEMBLY else "cif"
        with tempfile.TemporaryDirectory(prefix="apecx_pymol_") as tmp:
            workdir = Path(tmp)
            shutil.copy2(self._job_script, workdir / "_pymol_job.py")
            shutil.copy2(self._sasa_helper, workdir / "_pymol_sasa.py")
            shutil.copy2(structure_path, workdir / f"{pdb_id}.{ext}")
            job: dict[str, Any] = {
                "structure_path": f"/work/{pdb_id}.{ext}",
                "structure_kind": kind,
                "pdb_id": pdb_id,
                "conserved_regions": regions,
                "rsa_threshold": rsa_threshold,
                "min_map_identity": min_map_identity,
                "contact_cutoff": contact_cutoff,
                "render_png": f"/work/{pdb_id}.png",
            }
            if requested_chain:
                job["chain"] = requested_chain
            (workdir / "job.json").write_text(json.dumps(job))

            argv = self._docker_argv(workdir, memory_mb)
            async with acquire_container_slot():
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                try:
                    _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except TimeoutError as exc:
                    proc.kill()
                    await proc.communicate()
                    raise RuntimeError(f"PyMOL container exceeded {timeout:.0f}s timeout") from exc

            result_path = workdir / "result.json"
            if proc.returncode != 0 or not result_path.exists():
                full = stderr_b.decode("utf-8", errors="replace")
                stderr = (("…(truncated)…\n" + full[-8000:]) if len(full) > 8000 else full).strip()
                raise RuntimeError(
                    f"docker run exited {proc.returncode}; result.json "
                    f"{'present' if result_path.exists() else 'missing'}. stderr:\n{stderr}"
                )
            result = json.loads(result_path.read_text())
            # Provenance: stamp the built image's content digest so the SASA result records WHICH
            # container (by sha256, not just the mutable tag) computed it — HPC reproducibility.
            result["image_digest"] = self._digest
            viz = result.get("visualization_path")
            if viz:
                src = workdir / viz
                if src.exists():
                    try:
                        dest = Path(artifacts_dir) / viz
                        shutil.copy2(src, dest)
                        result["visualization_artifact"] = dest.name
                    except Exception as exc:  # noqa: BLE001 — artifact copy is best-effort
                        import logging

                        logging.getLogger(__name__).warning(
                            "structural viz copy failed for %s: %s", pdb_id, exc
                        )
            return result

    async def invoke(
        self,
        utd: UnifiedToolDescriptor,
        inputs: dict[str, Any],
        *,
        run_context_namespace: str = "",  # noqa: ARG002 — no per-tenant store
        **kwargs: Any,  # noqa: ARG002 — no pymol-specific backend kwargs
    ) -> dict[str, Any]:
        """Framework dispatch: validate the UTD-required inputs, run SASA, key the result under the
        UTD's single output name."""
        required = {i.name for i in (utd.inputs or []) if getattr(i, "required", False)}
        missing = required - set(inputs)
        if missing:
            raise ComponentConfigurationError(
                f"FAIL-FAST: PyMOLToolBackendAdapter missing required inputs: {sorted(missing)}"
            )
        result = await self.run_sasa(
            pdb_id=inputs["pdb_id"],
            structure_path=Path(inputs["structure_path"]),
            kind=inputs["structure_kind"],
            regions=inputs["conserved_regions"],
            rsa_threshold=inputs.get("rsa_threshold", 0.25),
            min_map_identity=inputs.get("min_map_identity", 0.7),
            contact_cutoff=inputs.get("contact_cutoff", 8.0),
            memory_mb=inputs.get("memory_mb", 2048),
            timeout=inputs.get("timeout", 300.0),
            artifacts_dir=Path(inputs.get("artifacts_dir", ".")),
            requested_chain=inputs.get("requested_chain"),
        )
        out_names = [o.name for o in (utd.outputs or [])]
        return {out_names[0]: result} if out_names else {"sasa_result": result}
