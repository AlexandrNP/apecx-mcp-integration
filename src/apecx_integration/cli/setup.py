"""apecx-setup orchestrator (2026-05-09; RAG made opt-in 2026-05-16, G81).

Single entry point for the entire APECx deployment recipe:

    pip install apecx-mcp-integration
    apecx-setup             # runs the default chain (no RAG, no Rhea)
    apecx-setup --with-rag  # default chain PLUS FAISS index build (~10 min)
    apecx-setup --with-rhea # default chain PLUS Rhea bring-up (~10 min one-time)
    apecx-setup globus      # only preflight Globus (G84)
    apecx-setup data        # only download VIOLIN + BV-BRC data
    apecx-setup infra       # only start Postgres + Redis containers
    apecx-setup llm         # only check/pull the Ollama model
    apecx-setup rag         # only build the FAISS RAG index (opt-in)
    apecx-setup rhea        # only run the Rhea one-time bring-up (opt-in, G89)
    apecx-setup verify      # only run the post-setup verification
    apecx-setup --reconfigure-llm   # change LLM env vars in existing config

Each subcommand is idempotent + safe to re-run. The default
(``apecx-setup``) runs the following in dependency order:
    1. ``globus`` — preflight Globus SDK + creds + endpoint UUIDs
                    (now REQUIRED for the data step; the gh fallback was
                    retired 2026-05-21)
    2. ``data``   — transfer VIOLIN + BV-BRC files via the Globus
                    verify→transfer workflow (sole path; fails loud if
                    Globus is unconfigured and no data is already local)
    3. ``infra``  — start Postgres + Redis containers if Docker is available
    4. ``llm``    — install Ollama if missing (interactive); start daemon;
                    pull the configured model
    5. ``verify`` — smoke-check every component reports healthy

Two slots in the chain are OPT-IN:
    * ``rag``  (G81, 2026-05-16) — FAISS index build for synthesis
                workflows. ~10 min, 689 MB. Default-skipped.
    * ``rhea`` (G89, 2026-05-16) — Rhea checkout sync + ingestion +
                embedding-model pull for bioinformatics tools (muscle,
                future Galaxy tools). ~10 min one-time. Default-skipped.
                After running once, apecx-mcp auto-discovers + auto-
                spawns the Rhea host process at every startup (G88).

The ``rag`` step (FAISS index build, 689 MB, ~10 min) is **opt-in**
since G81 (2026-05-16). The 80%-case (DB queries, MCP tools,
composer, HPC execution, synonym dictionary) runs without it; only
synthesis workflows that wire the domain RAG branch need it.
Pipelines that include RAG steps degrade gracefully when the index
is missing — no crashes, just empty RAG bundles + a loud "RAG
DISABLED" banner. Run ``apecx-setup rag`` or ``apecx-setup
--with-rag`` when you specifically need it.

Brutal-truth design notes:

- Every step gracefully degrades when the underlying optional
  capability is absent. The exit code captures whether the FULL
  setup succeeded — partial-success is reported via a summary
  table at the end.

- ``llm`` step CAN install Ollama interactively (2026-05-11 — was
  previously deferred to the user; the friction was real enough
  to justify the integration). The install is opt-in: the exact
  command is printed and a y/N prompt requires consent before any
  ``brew install`` or ``curl | sh`` runs. ``--non-interactive``
  mode SKIPS the install offer entirely (CI / scripted runs must
  install Ollama out-of-band).

- We still DO NOT install Docker or gh ourselves — those need
  the user's package manager. The setup tells the user EXACTLY
  what's missing and how to install it.

- ``apecx-setup verify`` is the most important subcommand for
  adoption: a single check that says "your stack is ready" vs.
  "this specific piece is missing — install it like so."

- The Docker containers are named with the ``apecx-`` prefix so
  they don't collide with the user's other containers. They are
  NOT auto-removed on apecx-setup re-run; the user controls
  container lifetime.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

# We delegate the data step to the existing setup_data implementation.
from apecx_integration.cli import setup_data as _setup_data
from apecx_integration.infrastructure.containers import (
    APECX_REDIS,
    APECX_RHEA_MINIO,
    APECX_RHEA_POSTGRES,
)

# ---------------------------------------------------------------------------
# Step result dataclass + summary table
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class StepResult:
    name: str
    status: str  # "ok" | "skipped" | "fail" | "partial"
    detail: str = ""

    @property
    def emoji(self) -> str:
        return {"ok": "✅", "skipped": "⏭ ", "partial": "⚠️ ", "fail": "❌"}.get(
            self.status,
            "  ",
        )


def _print_header(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _print_summary(results: list[StepResult]) -> int:
    print()
    print("=" * 60)
    print("  Setup summary")
    print("=" * 60)
    for r in results:
        print(f"  {r.emoji} {r.name:<10} {r.detail}")
    print()
    fail_count = sum(1 for r in results if r.status == "fail")
    partial_count = sum(1 for r in results if r.status == "partial")
    if fail_count:
        print(f"❌ {fail_count} step(s) failed. See messages above.")
        return 1
    if partial_count:
        print(f"⚠️  {partial_count} step(s) completed with warnings.")
        return 0
    print("✅ Stack is ready. Run ``apecx-setup verify`` any time to re-check.")
    return 0


# ---------------------------------------------------------------------------
# Step 1 — data
# ---------------------------------------------------------------------------


def _step_globus(*, interactive: bool = True) -> StepResult:
    """Globus install + configuration preflight (G84, 2026-05-16).

    Runs BEFORE ``_step_data`` so the operator sees Globus readiness
    surfaced as a first-class step in the summary table — not buried
    inside the data step's silent precondition check.

    Status semantics (per the workspace honesty contract):
      * ``ok``      — every Globus prerequisite is satisfied; the data
                      step will transfer via Globus.
      * ``skipped`` — at least one prerequisite is missing. Since the gh
                      fallback was retired (2026-05-21) Globus is now
                      REQUIRED for data acquisition: the data step will
                      FAIL unless the dataset is already present locally.
                      Kept as ``skipped`` (not ``fail``) here so the
                      preflight doesn't false-fail when data already exists
                      — the data step is the authoritative gate.
      * ``fail``    — preconditions appear met but the step's own
                      health probe raised. Reserved for the future
                      (e.g., when the step grows a live endpoint
                      reachability check). Currently unused.

    Interactive mode prints actionable instructions for each missing
    prerequisite (which env var to set, which ``apecx-globus-setup``
    command to run) but does NOT prompt to install/configure — that
    would turn ``apecx-setup`` into a guided wizard and the Globus
    setup is its own multi-minute flow with browser-based device
    auth. Operators run ``apecx-globus-setup`` separately and re-run
    ``apecx-setup`` when they're ready.

    Why a step (vs. just a check inside _step_data)
    ------------------------------------------------
    The user's directive (2026-05-16): "Globus installation and
    configuration should happen before file download." Surfacing
    Globus as its own step in the chain meets that directive in the
    operator-visible install flow. The summary table now reads:

      ✅ globus    SDK + creds + endpoints OK
      ✅ data      Globus: transferred 6 items (task_id=...)

    instead of the pre-G84 shape where Globus status was invisible
    until the data step printed its own line.
    """
    _print_header("Step 1 of 6 — Globus")
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    prereqs = check_globus_prerequisites()
    if prereqs.configured:
        return StepResult(
            "globus",
            "ok",
            "SDK + credentials + endpoint UUIDs all present",
        )

    # Print actionable per-prerequisite instructions in interactive mode.
    # Non-interactive (CI / scripted) mode just returns the structured
    # reason — the operator's surrounding tooling reads it.
    if interactive:
        print("  ▶  Globus preflight: not configured (see below)")
        if not prereqs.sdk_installed:
            print("     • globus_sdk missing — install with: pip install globus-sdk")
        if not prereqs.source_endpoint_set:
            print("     • APECX_GLOBUS_SOURCE_ENDPOINT_ID not set in env")
            print("       Ask the data steward for the source endpoint UUID,")
            print("       then `export APECX_GLOBUS_SOURCE_ENDPOINT_ID=<uuid>`.")
        if not prereqs.dest_endpoint_set:
            print("     • APECX_GLOBUS_DEST_ENDPOINT_ID not set in env")
            print("       Install Globus Connect Personal, grab the endpoint UUID")
            print(
                "       from Settings → Endpoints, then `export APECX_GLOBUS_DEST_ENDPOINT_ID=<uuid>`."
            )
        if not prereqs.credentials_reachable:
            print("     • no client credentials in env or keyring")
            print(
                "       Create a confidential client at https://app.globus.org/settings/developers"
            )
            print("       then store the credentials:")
            print("         apecx-globus-setup store --client-id <id> --client-secret <secret>")
        print()
        print("  ▶  Globus is now REQUIRED for data acquisition (the gh-release")
        print("     fallback was retired 2026-05-21). The data step will FAIL")
        print("     unless the dataset is already present locally.")
        print("     See docs/globus_data_transfer.md for the full setup recipe.")

    return StepResult(
        "globus",
        "skipped",
        prereqs.reason() + " (REQUIRED for data — gh fallback retired)",
    )


def _step_data(*, interactive: bool = True) -> StepResult:
    """Acquire the VIOLIN + BV-BRC dataset via Globus (sole path since 2026-05-21).

    The legacy ``gh release download`` fallback is RETIRED. Globus is now a hard
    requirement: ``globus_sdk`` + credentials + source/dest endpoint UUIDs must
    be set, and Globus Connect Personal must be running on the dest endpoint.

    Datasets split into REQUIRED (BV-BRC, on the public collection — verified,
    always reachable with the M2M creds) and OPTIONAL (VIOLIN, on the Group-gated
    `apecx-project-all` collection the transfer identity is not yet a member of).

    Flow:
      1. Non-interactive: skip (can't safely prompt). Reports whether the
         REQUIRED data is already present.
      2. Globus unconfigured + required data already present → skipped.
      3. Globus unconfigured + no required data → FAIL LOUD with actionable
         setup instructions (no silent degradation — the point of retiring gh).
      4. Globus configured → prompt for the data dir, then:
         - transfer REQUIRED (BV-BRC); any failure → step ``fail``.
         - transfer OPTIONAL (VIOLIN); failure → LOUD warning + step ``partial``
           (the install still COMPLETES — partial is exit 0). This is how a
           clean install succeeds on public data while VIOLIN access is pending.
         Then patch the Claude Desktop config.
    """
    _print_header("Step 2 of 6 — Data")
    from apecx_integration.cli._globus_data_transfer import (
        attempt_globus_data_transfer,
        check_globus_prerequisites,
    )

    default_data = _setup_data._DEFAULT_DATA_DIR
    # "Present" keys on the REQUIRED dataset (BV-BRC). VIOLIN is optional, so a
    # BV-BRC-only install still counts as "required data present".
    data_present = (default_data / "BVBRC_genome_alphavirus.csv").exists()

    if not interactive:
        if data_present:
            return StepResult(
                "data", "skipped", f"existing data at {default_data}; non-interactive mode"
            )
        return StepResult(
            "data",
            "skipped",
            "non-interactive mode + no existing data; run `apecx-setup data` interactively",
        )

    prereqs = check_globus_prerequisites()
    if not prereqs.configured:
        if data_present:
            return StepResult(
                "data",
                "skipped",
                f"Globus not configured, but dataset already present at {default_data}",
            )
        # Hard failure: gh fallback retired, Globus required, no data on disk.
        print(f"  ❌  Globus not configured: {prereqs.reason()}")
        print("     The gh-release fallback was retired 2026-05-21 — Globus is the")
        print("     only data-acquisition path now. Set up the missing pieces:")
        if not prereqs.sdk_installed:
            print("     • pip install globus-sdk")
        if not prereqs.source_endpoint_set:
            print("     • export APECX_GLOBUS_SOURCE_ENDPOINT_ID=<source collection UUID>")
            print("       (ask the data steward)")
        if not prereqs.dest_endpoint_set:
            print("     • Install Globus Connect Personal, then")
            print("       export APECX_GLOBUS_DEST_ENDPOINT_ID=<your personal endpoint UUID>")
        if not prereqs.credentials_reachable:
            print("     • apecx-globus-setup store --client-id <id> --client-secret <secret>")
        print("     Full recipe: docs/globus_data_transfer.md")
        return StepResult("data", "fail", f"Globus required but not configured: {prereqs.reason()}")

    # Configured — prompt for the data dir (relocated from the retired gh path).
    data_dir = _setup_data.prompt_for_data_dir(interactive=interactive)
    if data_dir is None:
        return StepResult("data", "skipped", "operator aborted at the data-directory prompt")
    data_dir.mkdir(parents=True, exist_ok=True)

    # REQUIRED — BV-BRC (public collection). Must succeed.
    print(f"  ▶  transferring REQUIRED data (BV-BRC) to {data_dir}")
    req = attempt_globus_data_transfer(data_dir=data_dir, datasets={"bvbrc"})
    if req.status != "ok":
        print(f"  ❌  Required BV-BRC transfer failed: {req.detail}")
        return StepResult("data", "fail", f"required BV-BRC transfer failed: {req.detail}")

    # OPTIONAL — VIOLIN (Group-gated 'apecx-project-all'; membership pending).
    # A failure here is NOT fatal: warn loudly and complete the install.
    print("  ▶  transferring OPTIONAL data (VIOLIN)")
    opt = attempt_globus_data_transfer(data_dir=data_dir, datasets={"violin"})

    # Report layout + patch the Claude Desktop config regardless (BV-BRC is in).
    _setup_data.report_post_transfer_layout(data_dir)
    _setup_data._maybe_update_claude_config(data_dir)

    if opt.status == "ok":
        detail = "Globus: BV-BRC + VIOLIN transferred"
        task_ids = [t for t in (req.task_id, opt.task_id) if t]
        if task_ids:
            detail += f" (task_ids={','.join(task_ids)})"
        return StepResult("data", "ok", detail)

    # VIOLIN unavailable — LOUD warning, but the install COMPLETES (partial =
    # exit 0). This is the requested "say loudly VIOLIN is missing, but the
    # entire setup completes successfully" behavior.
    print()
    print("  ⚠️  VIOLIN data was NOT transferred — this is OPTIONAL; the install CONTINUES.")
    print(f"        Reason: {opt.detail}")
    print("        Most likely the transfer identity is not yet a member of the")
    print("        'apecx-project-all' Globus Group (an admin grant is pending). BV-BRC")
    print("        data is installed and the stack is usable; VIOLIN-dependent lookups")
    print("        return empty until VIOLIN is fetched. Re-run `apecx-setup data` once")
    print("        Group access is granted.")
    return StepResult(
        "data", "partial", f"BV-BRC installed; VIOLIN skipped (optional): {opt.detail[:80]}"
    )


# ---------------------------------------------------------------------------
# Step 2 — infra (Docker containers)
# ---------------------------------------------------------------------------


# Container specs are now defined in ``apecx_integration.infrastructure.containers``
# so the orchestrator (startup-time bring-up) and this CLI (one-shot
# operator bring-up) share a single source of truth. The legacy
# ``apecx-postgres`` (postgres:16, port 5432) and ``apecx-redis`` specs
# from before 2026-05-15 are superseded by the rhea-stack equivalents
# (``apecx-rhea-postgres`` on port 5435, ``apecx-redis`` on 6379 still,
# plus ``apecx-rhea-minio``). The ``ready_check`` per-container shell
# command lives here because it's a CLI concern (docker exec) — the
# orchestrator uses real-probe paths (psycopg / redis-py / httpx)
# instead. ``purpose`` is the human-facing description.
def _spec_to_run_args(spec) -> list[str]:
    """Translate a ContainerSpec into ``-p H:C / -e K=V / -v SRC:DST`` argv.

    The ``-v`` flag is the load-bearing one for stateful services
    (Postgres, MinIO): without it apecx-setup would create no-volume
    containers and the operator's data would silently live in the
    container's ephemeral layer. Matches the shape emitted by
    ``apecx_integration.infrastructure.containers.container_run_args``
    so apecx-setup and the orchestrator produce equivalent containers.
    """
    args: list[str] = []
    for host, container in spec.ports:
        args.extend(["-p", f"{host}:{container}"])
    for key, value in spec.env:
        args.extend(["-e", f"{key}={value}"])
    for source, container_path in spec.volumes:
        args.extend(["-v", f"{source}:{container_path}"])
    return args


_DOCKER_CONTAINERS = [
    {
        "name": APECX_RHEA_POSTGRES.container_name,
        "image": APECX_RHEA_POSTGRES.image,
        "args": _spec_to_run_args(APECX_RHEA_POSTGRES),
        "command": list(APECX_RHEA_POSTGRES.command),
        "ready_check": [
            "pg_isready",
            "-U",
            "postgres",
            "-h",
            "localhost",
            "-d",
            dict(APECX_RHEA_POSTGRES.env).get("POSTGRES_DB", "postgres"),
        ],
        "purpose": "pgvector store for Rhea (vector search + caches)",
    },
    {
        "name": APECX_REDIS.container_name,
        "image": APECX_REDIS.image,
        "args": _spec_to_run_args(APECX_REDIS),
        "command": list(APECX_REDIS.command),
        "ready_check": ["redis-cli", "ping"],
        "purpose": "Redis cache + task queue (Rhea + apecx-mcp)",
    },
    {
        "name": APECX_RHEA_MINIO.container_name,
        "image": APECX_RHEA_MINIO.image,
        "args": _spec_to_run_args(APECX_RHEA_MINIO),
        "command": list(APECX_RHEA_MINIO.command),
        # MinIO has no `redis-cli`-style ready check; check the API
        # health-live endpoint via wget (busybox-style minimal probe).
        "ready_check": [
            "wget",
            "--quiet",
            "--spider",
            "http://localhost:9000/minio/health/live",
        ],
        "purpose": "MinIO object store for Rhea (S3-compatible)",
    },
]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def _container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-q", "-f", f"name={name}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return bool(result.stdout.strip())


def _container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-aq", "-f", f"name={name}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return bool(result.stdout.strip())


def _step_infra() -> StepResult:
    _print_header("Step 3 of 6 — Infrastructure (Docker containers)")
    if not _docker_available():
        return StepResult(
            "infra",
            "skipped",
            "docker daemon unreachable. Install Docker Desktop "
            "(https://docker.com/desktop) and start it.",
        )

    started: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for spec in _DOCKER_CONTAINERS:
        name = spec["name"]
        if _container_running(name):
            print(f"  ⏭  {name} already running ({spec['purpose']})")
            skipped.append(name)
            continue
        if _container_exists(name):
            # Stopped container with the same name — start it
            print(f"  ▶  starting existing {name} ...")
            result = subprocess.run(
                ["docker", "start", name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                started.append(name)
            else:
                failed.append(f"{name}: {result.stderr[:120]}")
            continue
        # Fresh start — pull + run
        print(f"  ▶  starting {name} from {spec['image']} ({spec['purpose']}) ...")
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            *spec["args"],
            spec["image"],
            *spec.get("command", []),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            failed.append(f"{name}: {result.stderr[:120]}")
            continue
        # Wait for ready
        deadline = time.time() + 30
        while time.time() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, *spec["ready_check"]],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ready.returncode == 0:
                started.append(name)
                break
            time.sleep(1)
        else:
            failed.append(f"{name}: did not become ready within 30s")

    if failed:
        return StepResult(
            "infra",
            "fail",
            f"started={started} skipped={skipped} FAILED={failed}",
        )
    if not started and skipped:
        return StepResult(
            "infra",
            "skipped",
            f"all containers already running: {skipped}",
        )
    return StepResult(
        "infra",
        "ok",
        f"started={started} already-running={skipped}",
    )


# ---------------------------------------------------------------------------
# Step 3 — LLM (Ollama model pull)
# ---------------------------------------------------------------------------


def _ollama_url() -> str:
    return os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/v1")


def _ollama_model() -> str:
    """Resolve the configured Ollama model.

    Default is ``mistral-nemo:latest`` because it has the longest
    track record on the composer's structured-YAML task in this
    workspace (T01 AC1 strict path, 3/3 consecutive RUN_COMPLETED).

    Other models we have a reason to mention:

      - ``mistral-small:latest`` (23B) — what composer_config.yml
        declares as its own default. Better instruction-following on
        long candidate blocks; ~14GB on disk.
      - ``gemma4:latest`` (8B) — Gemma 4 family (2026 release).
        Drop-in size with mistral-nemo (~9.6GB). **MEASURED 2026-05-11
        TO BE WORSE THAN mistral-nemo FOR THIS TASK**: 2×→4× more
        framework-rule violations on the diagnostic E2E test, 1.7×
        slower per inference. Stick with mistral-nemo as the
        composer default unless a future Gemma 4 fine-tune fixes the
        gap. Increase ``APECX_LLM_MAX_VALIDATION_RETRIES=2`` if you
        choose gemma4 anyway.
      - ``gemma4:26b`` — Mixture-of-Experts with 4B active params.
        Compute profile similar to gemma4:latest but with broader
        knowledge; ~16GB on disk. Not measured here.

    Override via ``APECX_LLM_MODEL`` env var. ``apecx-setup llm``
    pulls whatever you named. The diagnostic E2E test
    (``test_composer_validator_e2e_against_ollama.py``) is the
    regression gate when swapping models — verifies the
    structured-feedback machinery works regardless of LLM quality.
    """
    return os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _ollama_daemon_reachable(timeout: float = 2.0) -> bool:
    """Probe Ollama's /api/tags endpoint.

    Returns True when the daemon responds. Caller is responsible for
    deciding whether to start the daemon or report skipped.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(_ollama_url() + "/api/tags", timeout=timeout) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def _prompt_yes(question: str, default: bool = True) -> bool:
    """Prompt user for y/N. Returns the default on empty input."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  ▶  {question} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _offer_install_ollama(*, interactive: bool) -> bool:
    """Offer to install Ollama via the platform's documented installer.

    Returns True when ``ollama`` is on PATH after this call (already
    was, or just installed). Returns False when install was declined,
    impossible, or non-interactive.

    Platform-specific install commands (matching the official
    Ollama docs):

    - macOS: ``brew install ollama`` (preferred; clean uninstall path)
    - Linux: ``curl -fsSL https://ollama.ai/install.sh | sh``
      (the official Linux installer; sets up a systemd service)

    The exact command is printed BEFORE the y/N prompt so the
    operator sees what we're about to run. We never auto-install in
    ``--non-interactive`` mode — that mode is for CI / scripted runs
    where ``curl | sh`` and ``brew install`` are not the right
    surface; CI environments install Ollama out-of-band.
    """
    if shutil.which("ollama") is not None:
        return True

    if not interactive:
        return False

    import platform

    system = platform.system()
    print("\n  ⚠️  ``ollama`` CLI not found.")
    print("      The composer + RAG synthesis pipelines need an OpenAI-compatible LLM.")
    print(
        "      Skip this if you intend to use a remote endpoint "
        "(set ``APECX_LLM_BASE_URL`` to vLLM / OpenAI / Anthropic-proxy)."
    )
    print()

    if system == "Darwin":
        if shutil.which("brew") is None:
            print(
                "  ❌  Homebrew not found. Install brew from https://brew.sh "
                "first, then re-run ``apecx-setup llm``."
            )
            return False
        cmd: list[str] = ["brew", "install", "ollama"]
        print(f"  Proposed install command: {' '.join(cmd)}")
        if not _prompt_yes("Install Ollama via Homebrew?", default=True):
            return False
        result = subprocess.run(cmd, timeout=600)
        if result.returncode != 0:
            print(f"  ❌  ``{' '.join(cmd)}`` exited with {result.returncode}")
            return False
        return shutil.which("ollama") is not None

    if system == "Linux":
        # The Linux installer is `curl | sh` — print explicitly so
        # the operator sees the command before consenting. The URL
        # is hardcoded; no user-controlled interpolation.
        install_cmd = "curl -fsSL https://ollama.ai/install.sh | sh"
        print("  Proposed install command (runs `curl | sh` against the")
        print("  official Ollama installer):")
        print(f"    {install_cmd}")
        print(
            "  This downloads + executes a shell script. If you'd "
            "prefer to install manually, decline here and follow "
            "https://ollama.com/download"
        )
        if not _prompt_yes("Run the official Ollama install script?", default=False):
            return False
        result = subprocess.run(["sh", "-c", install_cmd], timeout=600)
        if result.returncode != 0:
            print(f"  ❌  Ollama install script exited with {result.returncode}")
            return False
        return shutil.which("ollama") is not None

    # Other platforms (Windows, BSD, etc.) — point at the manual installer.
    print(
        f"  ⚠️  Automatic install not supported on {system}. "
        f"Install manually from https://ollama.com/download "
        f"and re-run ``apecx-setup llm``."
    )
    return False


def _offer_start_ollama_daemon(*, interactive: bool) -> bool:
    """Start the Ollama daemon in the background.

    On Linux the official installer registers a systemd service that
    auto-starts; the daemon is usually already up after install. On
    macOS (Homebrew install) the daemon is NOT auto-started; we
    background ``ollama serve`` and poll for readiness.

    Returns True when the daemon is reachable after the attempt.
    """
    if _ollama_daemon_reachable():
        return True
    if not interactive:
        return False

    print("  ⚠️  Ollama daemon is not responding.")
    if not _prompt_yes("Start it in the background (`ollama serve &`)?", default=True):
        return False

    log_path = Path("/tmp/apecx-ollama-serve.log")
    try:
        # Detached background daemon. stdout/stderr -> log file so
        # the user can debug if the daemon fails to bind.
        with log_path.open("ab") as fp:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=fp,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from this terminal
            )
    except OSError as exc:
        print(f"  ❌  Failed to spawn `ollama serve`: {exc}")
        return False

    # Poll for readiness — Ollama takes 1-3 s to bind on a fresh start.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _ollama_daemon_reachable(timeout=1.0):
            print(f"  ✅  Ollama daemon up. Log: {log_path}")
            return True
        time.sleep(0.5)
    print(
        f"  ❌  Ollama daemon did not become reachable within 15s. "
        f"Check {log_path} for the daemon's stderr."
    )
    return False


def _step_llm(*, interactive: bool = True) -> StepResult:
    _print_header("Step 4 of 6 — LLM (Ollama install + check + model pull)")

    # 1. Ensure the CLI is installed (offer to install when missing).
    if not _offer_install_ollama(interactive=interactive):
        return StepResult(
            "llm",
            "skipped",
            "`ollama` CLI not found and install declined / not "
            "possible. Install from https://ollama.com/download (or "
            "set APECX_LLM_BASE_URL to a remote OpenAI-compatible "
            "endpoint to use vLLM / OpenAI / a hosted Anthropic-proxy).",
        )

    # 2. Ensure the daemon is reachable (offer to start when not).
    if not _offer_start_ollama_daemon(interactive=interactive):
        api_url = _ollama_url() + "/api/tags"
        return StepResult(
            "llm",
            "skipped",
            f"ollama daemon unreachable at {api_url}. Start with: ollama serve",
        )

    # 3. Ensure the model is pulled.
    import urllib.request

    api_url = _ollama_url() + "/api/tags"
    with urllib.request.urlopen(api_url, timeout=5) as resp:
        tags = json.loads(resp.read())
    model = _ollama_model()
    installed = {m.get("name") for m in tags.get("models") or []}
    if model in installed:
        return StepResult("llm", "ok", f"model {model} already pulled")

    print(f"  ▶  pulling {model} (this may take several minutes for first-time downloads)...")
    result = subprocess.run(
        ["ollama", "pull", model],
        timeout=1800,  # 30 minutes worst-case for ~14 GB models
    )
    if result.returncode != 0:
        return StepResult(
            "llm",
            "fail",
            f"`ollama pull {model}` exited with {result.returncode}",
        )
    return StepResult("llm", "ok", f"pulled {model}")


# ---------------------------------------------------------------------------
# Step 4 — RAG index
# ---------------------------------------------------------------------------


def _step_rag() -> StepResult:
    _print_header("Step 5 of 6 — RAG index (FAISS, opt-in)")
    repo_root = Path(__file__).resolve().parents[3]
    workspace_root = repo_root.parent
    domain_rag_dir = workspace_root / "data" / "apecx_domain_rag"
    index_file = domain_rag_dir / "faiss_index.bin"
    composer_cfg = repo_root / "src" / "apecx_integration" / "composition" / "composer_config.yml"

    if index_file.exists():
        return StepResult(
            "rag",
            "skipped",
            f"existing FAISS index at {index_file}",
        )

    if not composer_cfg.exists():
        return StepResult(
            "rag",
            "skipped",
            f"composer_config.yml not found at {composer_cfg}; can't build index",
        )

    build_script = repo_root / "scripts" / "build_rag_index.py"
    if not build_script.exists():
        return StepResult(
            "rag",
            "skipped",
            f"build script not found at {build_script}",
        )

    print(f"  ▶  building FAISS index from {composer_cfg} ...")
    venv_python = repo_root / ".venv" / "bin" / "python"
    python_cmd = str(venv_python) if venv_python.exists() else sys.executable
    nb_root = workspace_root / "nanobrain"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{nb_root}:{repo_root / 'src'}" + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    result = subprocess.run(
        [python_cmd, str(build_script), str(composer_cfg)],
        env=env,
        timeout=600,
    )
    if result.returncode != 0:
        return StepResult(
            "rag",
            "fail",
            f"build_rag_index.py exited with {result.returncode}",
        )
    if not index_file.exists():
        return StepResult(
            "rag",
            "partial",
            f"build script returned 0 but {index_file} still absent",
        )
    return StepResult("rag", "ok", f"built {index_file}")


# ---------------------------------------------------------------------------
# Step 5b — rhea (opt-in, idempotent one-time bring-up of the Rhea checkout)
# ---------------------------------------------------------------------------


def _step_rhea() -> StepResult:
    """One-time Rhea bring-up (G89, 2026-05-16).

    Idempotent. Safe to re-run.

    Phases:
      1. Locate the Rhea checkout (apecx-mcp-integration's
         ``rhea_env_autodiscovery._find_rhea_repo`` — same probe
         apecx-mcp uses at startup).
      2. Ensure rhea's venv exists (``uv sync && uv pip install -e .``).
         Skipped when ``.venv/bin/python`` is already present + the
         editable install is registered.
      3. Ensure mxbai-embed-large is pulled in Ollama (rhea's
         embedding backend). Skipped when ``ollama list`` already
         shows it.
      4. Ensure the ingestion has been run at least once
         (``rhea.preprocess.update_tools`` for whatever
         ``$RHEA_INGEST_ONLY`` (default ``muscle`` if unset) wants).
         Skipped when the rhea-postgres galaxytools table already
         has rows for the requested tools.

    After this step, apecx-mcp's existing rhea auto-spawn (driven by
    InfraOrchestrator's rhea_mcp BackendSpec) will engage on next
    startup with no operator-side env-var exports required — the
    G88 autodiscovery sets RHEA_REPO_PATH + RHEA_PYTHON_PATH from
    the checkout + venv this step produced.

    Why opt-in
    ----------
    The full Rhea bring-up costs ~10 minutes (uv sync builds the
    Parsl/Academy/proxystore stack; mxbai-embed-large is ~700 MB
    Ollama pull; first muscle ingestion is ~10 s). Operators who
    don't want Rhea-backed tools (muscle, future Galaxy tools) skip
    it. Same opt-in pattern as `_step_rag`.
    """
    _print_header("Step 5b of 6 — Rhea (host MCP server, opt-in)")

    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        _find_rhea_repo,
    )

    rhea_repo = _find_rhea_repo()
    if rhea_repo is None:
        return StepResult(
            "rhea",
            "skipped",
            (
                "no rhea checkout found in standard locations; "
                "git clone https://github.com/AlexandrNP/rhea.git into the "
                "workspace next to apecx-mcp-integration/ to enable"
            ),
        )

    print(f"  ▶  found rhea checkout at {rhea_repo}")

    # Phase 2: uv sync + editable install. We invoke uv via shutil.which
    # so an operator without uv on PATH gets a clear error rather than
    # subprocess gibberish.
    uv_binary = shutil.which("uv")
    if uv_binary is None:
        return StepResult(
            "rhea",
            "fail",
            "uv not on PATH — install from https://docs.astral.sh/uv/ then re-run",
        )

    venv_python = rhea_repo / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print("  ▶  uv sync (this may take 1-2 min on first run) ...")
        result = subprocess.run(
            [uv_binary, "sync"],
            cwd=rhea_repo,
            timeout=600,
        )
        if result.returncode != 0:
            return StepResult(
                "rhea",
                "fail",
                f"`uv sync` exited with {result.returncode}",
            )

    print("  ▶  uv pip install -e . (editable install of rhea-mcp) ...")
    result = subprocess.run(
        [uv_binary, "pip", "install", "-e", "."],
        cwd=rhea_repo,
        timeout=300,
    )
    if result.returncode != 0:
        return StepResult(
            "rhea",
            "fail",
            f"`uv pip install -e .` exited with {result.returncode}",
        )

    # Phase 3: ensure mxbai-embed-large is pulled. Ollama is the
    # ALSO embedding backend rhea uses; if the model is missing the
    # rhea ingestion step would fail downstream.
    ollama_binary = shutil.which("ollama")
    if ollama_binary is not None:
        listed = subprocess.run(
            [ollama_binary, "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if "mxbai-embed-large" not in listed.stdout:
            print("  ▶  pulling mxbai-embed-large (~700 MB) ...")
            pull = subprocess.run(
                [ollama_binary, "pull", "mxbai-embed-large"],
                timeout=900,
            )
            if pull.returncode != 0:
                return StepResult(
                    "rhea",
                    "partial",
                    f"`ollama pull mxbai-embed-large` exited with {pull.returncode}; "
                    "rhea ingestion will fail until you pull it manually",
                )
        else:
            print("  ▶  mxbai-embed-large already present in Ollama")
    else:
        print(
            "  ▶  ollama not on PATH — skipping embedding-model pull (operator must do this manually)"
        )

    # Phase 4: ensure the muscle tool (or whatever RHEA_INGEST_ONLY
    # asks for) is ingested. We don't try to be clever about
    # incremental ingestion — rhea's ingestion is idempotent
    # (upsert by primary key) so re-running just re-embeds at small
    # cost. We DO skip when the galaxytools table has rows for the
    # requested tool already — the typical case after the first
    # apecx-setup rhea run.
    ingest_only = os.environ.get("RHEA_INGEST_ONLY", "muscle")
    print(f"  ▶  running rhea ingestion (RHEA_INGEST_ONLY={ingest_only}) ...")
    ingest_env = os.environ.copy()
    ingest_env.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5435/rhea",
    )
    ingest_env.setdefault("EMBEDDING_URL", "http://localhost:11434/v1")
    ingest_env.setdefault("MODEL", "mxbai-embed-large")
    ingest_env["RHEA_INGEST_ONLY"] = ingest_only
    result = subprocess.run(
        [str(venv_python), "-m", "rhea.preprocess.update_tools"],
        cwd=rhea_repo,
        env=ingest_env,
        timeout=600,
    )
    if result.returncode != 0:
        return StepResult(
            "rhea",
            "partial",
            f"`rhea.preprocess.update_tools` exited with {result.returncode}; "
            "is apecx-rhea-postgres running? (run `apecx-setup infra` first)",
        )

    return StepResult(
        "rhea",
        "ok",
        f"venv + ingestion ready at {rhea_repo}; apecx-mcp will auto-spawn rhea-server on next start",
    )


# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------


def _step_verify() -> StepResult:
    _print_header("Step 6 of 6 — Verification")
    workspace_root = Path(__file__).resolve().parents[4]
    checks: list[tuple[str, bool, str]] = []

    # Data — BV-BRC is REQUIRED (public collection); VIOLIN is OPTIONAL
    # (Group-gated, membership pending). Report them separately so a
    # VIOLIN-missing install verifies as 'partial', not 'fail'.
    default_data = _setup_data._DEFAULT_DATA_DIR
    bvbrc_present = (default_data / "BVBRC_genome_alphavirus.csv").exists()
    checks.append(
        (
            "data",
            bvbrc_present,
            f"BV-BRC data at {default_data}"
            if bvbrc_present
            else "missing — run `apecx-setup data`",
        )
    )
    violin_present = (default_data / "violin" / "Vaccine_Information.csv").exists()
    checks.append(
        (
            "violin",
            violin_present,
            f"VIOLIN data at {default_data}/violin"
            if violin_present
            else "missing (optional) — pending 'apecx-project-all' Globus Group access",
        )
    )

    # Postgres (apecx-rhea-postgres — pgvector on host port 5435).
    pg_running = _docker_available() and _container_running(APECX_RHEA_POSTGRES.container_name)
    checks.append(
        (
            "postgres",
            pg_running,
            f"container {APECX_RHEA_POSTGRES.container_name} responsive"
            if pg_running
            else "not running — `apecx-setup infra` (or skip if not using PostgresTaskStore)",
        )
    )

    # Redis (apecx-redis on 6379).
    redis_running = _docker_available() and _container_running(APECX_REDIS.container_name)
    checks.append(
        (
            "redis",
            redis_running,
            f"container {APECX_REDIS.container_name} responsive"
            if redis_running
            else "not running — `apecx-setup infra` (or skip if not using Redis backend)",
        )
    )

    # MinIO (apecx-rhea-minio on 9000/9001).
    minio_running = _docker_available() and _container_running(APECX_RHEA_MINIO.container_name)
    checks.append(
        (
            "minio",
            minio_running,
            f"container {APECX_RHEA_MINIO.container_name} responsive"
            if minio_running
            else "not running — `apecx-setup infra` (or skip if Rhea object-store paths are not used)",
        )
    )

    # Ollama
    ollama_ok = False
    ollama_detail = "ollama CLI absent"
    if shutil.which("ollama") is not None:
        try:
            import urllib.request

            with urllib.request.urlopen(_ollama_url() + "/api/tags", timeout=5) as resp:
                tags = json.loads(resp.read())
            installed = {m.get("name") for m in tags.get("models") or []}
            if _ollama_model() in installed:
                ollama_ok = True
                ollama_detail = f"model {_ollama_model()} ready"
            else:
                ollama_detail = f"model {_ollama_model()} not pulled — `apecx-setup llm`"
        except Exception:  # noqa: BLE001
            ollama_detail = "daemon unreachable — `ollama serve`"
    checks.append(("ollama", ollama_ok, ollama_detail))

    # RAG index
    index_file = workspace_root / "data" / "apecx_domain_rag" / "faiss_index.bin"
    rag_ok = index_file.exists()
    checks.append(
        (
            "faiss",
            rag_ok,
            f"index at {index_file}" if rag_ok else "missing — `apecx-setup rag`",
        )
    )

    # Rhea (G89): check that the bring-up has been done. We don't probe
    # rhea-server reachability here (that's an apecx-mcp startup
    # concern; `InfraOrchestrator` handles it). We check the static
    # state apecx-setup rhea would have produced: checkout + venv +
    # ingestion.
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        _find_rhea_repo,
    )

    rhea_repo = _find_rhea_repo()
    if rhea_repo is None:
        rhea_ok = False
        rhea_detail = "no checkout found — `apecx-setup rhea` (opt-in)"
    elif not (rhea_repo / ".venv" / "bin" / "python").exists():
        rhea_ok = False
        rhea_detail = f"checkout at {rhea_repo} but no venv — `apecx-setup rhea`"
    else:
        rhea_ok = True
        rhea_detail = f"checkout + venv ready at {rhea_repo}"
    checks.append(("rhea", rhea_ok, rhea_detail))

    print()
    for name, ok, detail in checks:
        emoji = "✅" if ok else "❌"
        print(f"  {emoji} {name:<10} {detail}")
    print()

    failed = [name for name, ok, _ in checks if not ok]
    if not failed:
        return StepResult(
            "verify",
            "ok",
            "every component healthy",
        )
    # Postgres + Redis + MinIO are optional for many workflows; reflect
    # that honestly in the partial-vs-fail distinction. faiss + rhea
    # are also optional (opt-in per G81 + G89).
    optional = {"violin", "postgres", "redis", "minio", "faiss", "rhea"}
    real_failures = [f for f in failed if f not in optional]
    if real_failures:
        return StepResult(
            "verify",
            "fail",
            f"required components not healthy: {real_failures}",
        )
    return StepResult(
        "verify",
        "partial",
        f"optional components missing: {failed}",
    )


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

_SUBCOMMANDS: dict[str, Callable[..., StepResult]] = {
    "globus": _step_globus,
    "infra": lambda **_: _step_infra(),
    "llm": _step_llm,
    "rag": lambda **_: _step_rag(),
    "rhea": lambda **_: _step_rhea(),
    "verify": lambda **_: _step_verify(),
}


def _run_all(
    *,
    interactive: bool = True,
    with_rag: bool = False,
    with_rhea: bool = False,
) -> int:
    """Run the canonical install chain.

    Chain (G81 + G82 + G84; gh fallback retired 2026-05-21):
      1. globus  — preflight: SDK + credentials + endpoint UUIDs.
                   ``skipped`` when not configured (operator gets
                   actionable instructions); now REQUIRED for the data step.
      2. data    — VIOLIN/BV-BRC CSVs via the Globus verify→transfer
                   workflow (sole path; FAILS LOUD if Globus is unconfigured
                   and no data is already present locally).
      3. infra   — Docker containers (Postgres, Redis, MinIO)
      4. llm     — Ollama or remote LLM credentials
      5. verify  — sanity checks across all installed components

    The RAG (FAISS) step is **opt-in** as of G81: it's a ~10-minute
    build of a 689 MB index that's only needed by synthesis workflows
    that wire the domain RAG branch. Skipped from the default chain
    so first-time installs are fast and operators see immediate
    success for the 80%-case (DB queries, MCP tools, composer,
    HPC execution, synonym dictionary — all run without RAG).

    Pass ``with_rag=True`` (via ``apecx-setup --with-rag`` or run
    ``apecx-setup rag`` separately) when you specifically need the
    synthesis RAG branch.
    """
    print("apecx-setup — orchestrated stack initializer")
    print()

    results: list[StepResult] = []
    # G84: globus preflight runs BEFORE data so its status is
    # surfaced in the summary table. _step_data inspects this result
    # via its own precondition probe (no need to thread state — they
    # both call check_globus_prerequisites; the cost is two cheap
    # stat-only checks, not a network round-trip).
    results.append(_step_globus(interactive=interactive))
    results.append(_step_data(interactive=interactive))
    results.append(_step_infra())
    results.append(_step_llm(interactive=interactive))
    if with_rag:
        results.append(_step_rag())
    else:
        results.append(
            StepResult(
                "rag",
                "skipped",
                "opt-in — run `apecx-setup rag` or `apecx-setup --with-rag` to build the FAISS index (~10 min, 689 MB)",
            )
        )
    if with_rhea:
        results.append(_step_rhea())
    else:
        results.append(
            StepResult(
                "rhea",
                "skipped",
                "opt-in — run `apecx-setup rhea` or `apecx-setup --with-rhea` for Rhea-backed bioinformatics tools (~10 min one-time)",
            )
        )
    results.append(_step_verify())

    return _print_summary(results)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="apecx-setup",
        description=(
            "APECx stack orchestrator. Run with no args to set up "
            "every component idempotently. Use a subcommand to run "
            "only one step. --reconfigure-llm changes LLM env vars "
            "in an existing Claude Desktop config without re-downloading data."
        ),
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["globus", "data", "infra", "llm", "rag", "rhea", "verify", "all"],
        default="all",
        help="Step to run (default: all).",
    )
    parser.add_argument(
        "--reconfigure-llm",
        action="store_true",
        help="Re-prompt for LLM env vars in an existing config; skip data download.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip prompts (for CI / automation). Data step skips when no existing data.",
    )
    parser.add_argument(
        "--with-rag",
        action="store_true",
        help=(
            "Include the FAISS RAG index build in the default chain "
            "(G81: opt-in since 2026-05-16; ~10 min build, 689 MB index). "
            "Run this when you specifically need the synthesis RAG branch."
        ),
    )
    parser.add_argument(
        "--with-rhea",
        action="store_true",
        help=(
            "Include the Rhea bring-up (uv sync + ingestion + embedding "
            "model pull) in the default chain (G89: opt-in since "
            "2026-05-16; ~10 min one-time). Run this if you want the "
            "Rhea-backed bioinformatics tools (muscle, future Galaxy "
            "tools) available via the apecx-mcp catalog."
        ),
    )
    args = parser.parse_args(argv)

    if args.reconfigure_llm:
        _setup_data._run_reconfigure_llm()
        return

    if args.subcommand in (None, "all"):
        sys.exit(
            _run_all(
                interactive=not args.non_interactive,
                with_rag=args.with_rag,
                with_rhea=args.with_rhea,
            )
        )
    elif args.subcommand == "data":
        result = _step_data(interactive=not args.non_interactive)
    else:
        result = _SUBCOMMANDS[args.subcommand](
            interactive=not args.non_interactive,
        )

    sys.exit(_print_summary([result]))


if __name__ == "__main__":  # pragma: no cover
    main()
