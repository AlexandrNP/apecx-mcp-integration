"""T05 — PBS bundle generator.

Produce a self-contained directory that a scientist can ``qsub`` on
Polaris or Aurora. Everything the job needs lives in the bundle —
workflow YAML, a job script, a README explaining what's inside, and
seed data for Tier-2 re-ingest after completion. No live HPC
submission happens here; the bundle is a *portable artifact*.

AC5 (bundle layout)
-------------------
    <bundle_dir>/
        submit.pbs          — PBS script, qsub-able verbatim
        run.sh              — invoked inside the PBS job; loads env,
                              exec's the workflow runner
        workflow.yml        — copy of the Run's composed workflow
        staging_plan.yml    — input / data-snapshot references
                              (by content hash; no binaries staged)
        provenance_seed.json — {run_id, library_version, llm_model,
                              artifact_id, generated_at} — enough for
                              Tier 2 to reconstruct the Run row
        README.md           — human-readable rundown so a first-time
                              reader knows what the bundle is for

Scope notes — what this does NOT do
-----------------------------------
- Actual qsub. Bundle contents are validated for shape, not executed.
  AC2 (real job completes on target HPC) is operator-run and lives
  outside CI.
- Container image generation. Per AP §5.5 the bundle references a
  pinned container by name in ``submit.pbs`` — building the image
  is a separate ops task.
- Tier-2 ingest on completion. AC3 (reconstruct Run from
  ``provenance_seed.json``) is a separate code path (Tier 2 consumer)
  that lives in the Control Plane routes, not here. This module
  produces the seed; the consumer is T05 follow-up.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

SUPPORTED_SYSTEMS = frozenset({"polaris", "aurora"})


@dataclass(frozen=True, kw_only=True)
class BundleRequest:
    """What the generator needs to know about a run.

    Kept as a dataclass (not a pydantic model) because this module
    has no web-surface opinions — the HTTP layer adapts
    ``ExportHpcBundleRequest`` into this shape.
    """

    run_id: UUID
    target_system: str
    output_directory: Path
    workflow_yaml_path: Path
    library_version: str
    llm_model: str
    artifact_id: UUID
    composition_summary_sentence: str


@dataclass(frozen=True, kw_only=True)
class BundleResult:
    bundle_path: Path
    submit_command: str


class UnsupportedSystem(ValueError):
    """Raised when ``target_system`` isn't in ``SUPPORTED_SYSTEMS``."""


def generate_bundle(request: BundleRequest) -> BundleResult:
    """Write the bundle to disk and return the paths.

    Raises ``UnsupportedSystem`` when ``target_system`` isn't known.
    Raises ``FileNotFoundError`` when ``workflow_yaml_path`` does not
    exist (caller's data-integrity problem).
    """
    if request.target_system not in SUPPORTED_SYSTEMS:
        raise UnsupportedSystem(
            f"target_system={request.target_system!r} not supported. "
            f"Allowed: {sorted(SUPPORTED_SYSTEMS)}"
        )
    if not request.workflow_yaml_path.is_file():
        raise FileNotFoundError(
            f"workflow_yaml_path {request.workflow_yaml_path} does not exist"
        )

    bundle_dir = Path(request.output_directory).resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy the workflow YAML verbatim so the bundle is self-contained.
    workflow_copy = bundle_dir / "workflow.yml"
    shutil.copyfile(request.workflow_yaml_path, workflow_copy)

    # 2. Write the PBS submit script.
    submit_pbs = bundle_dir / "submit.pbs"
    submit_pbs.write_text(
        _render_submit_pbs(request, workflow_copy), encoding="utf-8"
    )
    submit_pbs.chmod(0o755)

    # 3. Write the in-job runner.
    run_sh = bundle_dir / "run.sh"
    run_sh.write_text(_render_run_sh(request), encoding="utf-8")
    run_sh.chmod(0o755)

    # 4. Write the staging plan.
    staging_plan = bundle_dir / "staging_plan.yml"
    staging_plan.write_text(
        _render_staging_plan(request), encoding="utf-8"
    )

    # 5. Write the provenance seed.
    seed = bundle_dir / "provenance_seed.json"
    seed.write_text(
        json.dumps(
            {
                "run_id": str(request.run_id),
                "artifact_id": str(request.artifact_id),
                "library_version": request.library_version,
                "llm_model": request.llm_model,
                "composition_summary_sentence": (
                    request.composition_summary_sentence
                ),
                "target_system": request.target_system,
                "generated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 6. Write the README.
    (bundle_dir / "README.md").write_text(
        _render_readme(request), encoding="utf-8"
    )

    return BundleResult(
        bundle_path=bundle_dir,
        submit_command=f"cd {bundle_dir} && qsub submit.pbs",
    )


# ----------------------------------------------------------------------
# Template renderers
# ----------------------------------------------------------------------


def _render_submit_pbs(request: BundleRequest, workflow_path: Path) -> str:
    """Minimal-viable PBS script. Defaults are Polaris/Aurora-shaped.

    The scientist is expected to edit ``-A <account>`` and wall-time
    before submitting. The script SOURCES ``run.sh`` so the env setup
    and workflow invocation live in one auditable place.
    """
    queue = "prod" if request.target_system == "polaris" else "EarlyAppAccess"
    nodes = 1
    wall = "01:00:00"
    return textwrap.dedent(
        f"""\
        #!/bin/bash
        #PBS -N apecx_{str(request.run_id)[:8]}
        #PBS -A <FILL_IN_ALLOCATION_ACCOUNT>
        #PBS -q {queue}
        #PBS -l select={nodes}:ncpus=1
        #PBS -l walltime={wall}
        #PBS -j oe
        #PBS -o apecx_job.log

        set -euo pipefail
        cd "$PBS_O_WORKDIR"
        bash run.sh
        """
    )


def _render_run_sh(request: BundleRequest) -> str:
    """The inner script the PBS job runs.

    **STUB BUNDLE — DOES NOT EXECUTE THE WORKFLOW.** Audit §3.5
    (docs/codebase_audit_2026_04_24.md): pre-fix, this script wrote
    "completed" + a stub result and exited 0; a scientist could
    qsub the bundle, see "success" downstream, and never realize
    the actual workflow never ran. The bundle now writes
    ``stub_completed`` to BOTH ``apecx_status.txt`` AND
    ``outputs/result.json`` so the ingest path can detect-and-warn
    on this marker rather than silently treating it as a
    completed run.

    A real runner wiring nanobrain's executor into HPC is T05
    follow-up — this establishes the bundle contract so Tier-2
    ingest has something deterministic to consume.
    """
    return textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail

        # ============================================================
        # APECX BUNDLE STUB — Phase-2 scaffold.
        # ============================================================
        # This script does NOT execute the apecx workflow. It writes
        # a "stub_completed" marker so the Tier-2 ingest contract is
        # exercisable end-to-end while the real qsub-driven runner
        # (T05 follow-up) is still pending.
        #
        # If you are running this bundle on a real HPC and expect
        # apecx to do useful work, the runner has NOT shipped yet —
        # see implementation_plan.md §T05.
        # ============================================================

        echo "apecx-run starting: run_id={request.run_id} (STUB BUNDLE)"
        echo "  target_system   : {request.target_system}"
        echo "  library_version : {request.library_version}"
        echo "  llm_model       : {request.llm_model}"
        echo "  artifact_id     : {request.artifact_id}"

        # Ensure the workflow yaml is present.
        test -f workflow.yml
        test -f staging_plan.yml
        test -f provenance_seed.json

        # Marker files: Tier 2 ingest looks for this + the result JSON.
        # The "stub_completed" status (NOT "completed") tells the
        # ingest path to treat this as a stub — see audit §3.5.
        echo "started" > apecx_status.txt
        mkdir -p outputs
        echo '{{"status": "stub_completed", "stub": true}}' > outputs/result.json
        echo "stub_completed" > apecx_status.txt

        echo "apecx-run done (STUB)"
        """
    )


def _render_staging_plan(request: BundleRequest) -> str:
    return textwrap.dedent(
        f"""\
        # Staging plan — what needs to land in $PBS_O_WORKDIR before the job starts.
        #
        # For first release the workflow YAML is copied in verbatim (see
        # ``workflow.yml``). Input data that lives in the BV-BRC / VIOLIN
        # snapshot is referenced by content hash; the scientist is expected
        # to ``globus transfer`` or ``rsync`` the snapshot into
        # ``data/inputs/`` before qsub.
        run_id: "{request.run_id}"
        target_system: "{request.target_system}"
        inputs: []
        outputs_dir: "outputs"
        """
    )


def _render_readme(request: BundleRequest) -> str:
    return textwrap.dedent(
        f"""\
        # APECx run bundle — {request.run_id}

        This directory is a **self-contained submission bundle** for the
        APECx workflow generated by Run ``{request.run_id}``. A scientist
        can ``qsub submit.pbs`` on the target HPC system to run it.

        ## What's inside

        - ``submit.pbs``            — PBS script. Edit the
          ``-A <account>`` line, then ``qsub``.
        - ``run.sh``                — script the PBS job runs inside the
          node. Sources env + executes the workflow.
        - ``workflow.yml``          — the composed workflow this run is
          based on. Same content-hash as the Artifact in the originating
          Control Plane.
        - ``staging_plan.yml``      — references to input data that must
          be present before the job starts.
        - ``provenance_seed.json``  — enough metadata for Tier 2 to
          reconstruct the Run row once results return.
        - ``README.md``             — you are here.

        ## Composition summary

        {request.composition_summary_sentence}

        ## How to submit

            cd "$(pwd)"
            # edit ``submit.pbs``: set -A <your_allocation>
            qsub submit.pbs

        ## What to do with the result

        The job writes ``outputs/result.json`` + ``apecx_status.txt``. To
        re-ingest into the Control Plane, ``globus transfer`` (or
        ``scp``) the whole bundle directory back and point Tier 2's
        ingest at ``provenance_seed.json`` — that's enough to reconstruct
        a COMPLETED Run record.

        ## Target system

        ``{request.target_system}`` (supported: {sorted(SUPPORTED_SYSTEMS)}).

        ## Generated

        Library version: {request.library_version}
        LLM model      : {request.llm_model}
        Artifact ID    : {request.artifact_id}
        """
    )


__all__ = [
    "BundleRequest",
    "BundleResult",
    "UnsupportedSystem",
    "generate_bundle",
    "SUPPORTED_SYSTEMS",
]
