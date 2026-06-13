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
                    (skipped cleanly when not configured — operators
                    who don't use Globus see no extra friction)
    2. ``data``   — download VIOLIN + BV-BRC files (Globus when the
                    preflight said OK; falls back to ``gh release``)
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
      * ``skipped`` — at least one prerequisite is missing; the data
                      step will fall back to ``gh release download``.
                      This is NOT a failure: operators who don't use
                      Globus see no extra friction.
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
        print("  ▶  data step will use the gh release download fallback")
        print("     See docs/globus_data_transfer.md for the full setup recipe.")

    return StepResult(
        "globus",
        "skipped",
        prereqs.reason() + " (data step will use gh release fallback)",
    )


def _step_data(*, interactive: bool = True, prefer_gh_release: bool = False) -> StepResult:
    """Acquire the VIOLIN + BV-BRC dataset. G82 (2026-05-16): Globus-first.

    Path selection
    --------------
    1. ``prefer_gh_release=True`` (operator flag): always use the
       existing ``gh release download`` path; never touch Globus.
    2. Otherwise: check Globus preconditions
       (``check_globus_prerequisites``). If they pass, drive
       ``GlobusTransferStep`` via the wrapper YAML and call it a day.
       If the transfer fails AT THE NETWORK LAYER (Globus auth error,
       endpoint unreachable, task failed), fall back to gh release —
       the user wanted data, not a Globus debugging session.
    3. If Globus preconditions don't pass (no SDK, no env vars, no
       credentials), fall back to gh release silently. Operators who
       never set up Globus see no extra friction.

    The fallback is also the canonical historical path: ``apecx-data``
    GitHub release with the 6 CSVs in a tarball. Same content; just
    fetched over GitHub instead of Globus.
    """
    _print_header("Step 2 of 6 — Data")
    if not interactive:
        # Non-interactive mode: skip if data already present at the
        # default location. We can't safely auto-prompt the user for
        # a directory in non-interactive mode.
        default_data = _setup_data._DEFAULT_DATA_DIR
        if (default_data / "violin" / "Vaccine_Information.csv").exists():
            return StepResult(
                "data",
                "skipped",
                f"existing data at {default_data}; non-interactive mode",
            )
        return StepResult(
            "data",
            "skipped",
            "non-interactive mode + no existing data; run `apecx-setup data` interactively",
        )

    # ----- Globus-first attempt -----
    # Skip Globus only if the operator forced it OR if preconditions
    # are missing. We surface either case in the step result so the
    # summary table is honest about which path ran.
    if not prefer_gh_release:
        from apecx_integration.cli._globus_data_transfer import (
            attempt_globus_data_transfer,
            check_globus_prerequisites,
        )

        prereqs = check_globus_prerequisites()
        if prereqs.configured:
            data_dir = _setup_data._DEFAULT_DATA_DIR
            data_dir.mkdir(parents=True, exist_ok=True)
            print(f"  ▶  attempting Globus transfer to {data_dir}")
            result = attempt_globus_data_transfer(data_dir=data_dir)
            if result.status == "ok":
                detail = f"Globus: {result.detail}"
                if result.task_id:
                    detail += f" (task_id={result.task_id})"
                return StepResult("data", "ok", detail)
            # Globus attempted but failed. Tell the operator + try gh.
            print(f"  ⚠️  Globus transfer failed: {result.detail}")
            print("     falling back to gh release download")
        else:
            # Preconditions unmet — silent fallback to gh.
            print(f"  ▶  Globus not configured ({prereqs.reason()})")
            print("     using gh release download instead")

    # ----- gh release fallback -----
    try:
        _setup_data._run_full_setup()
    except SystemExit as exc:
        if exc.code == 0:
            return StepResult("data", "ok", "gh release: downloaded + extracted")
        return StepResult(
            "data",
            "fail",
            f"setup_data exited with code {exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult("data", "fail", f"{type(exc).__name__}: {exc}")
    return StepResult("data", "ok", "gh release: downloaded + extracted")


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


def _bring_up_containers(
    specs: list[dict],
) -> tuple[list[str], list[str], list[str]]:
    """Idempotently start a set of container specs (the ``_DOCKER_CONTAINERS``
    shape). Returns ``(started, already_running, failed)``.

    Shared by ``_step_infra`` and ``_step_rhea`` so the sidecar bring-up
    has a single source of truth — ``_step_rhea`` reuses the SAME three
    sidecar specs (Postgres/Redis/MinIO) the orchestrator + infra step
    use; it never re-declares them.
    """
    started: list[str] = []
    already: list[str] = []
    failed: list[str] = []

    for spec in specs:
        name = spec["name"]
        if _container_running(name):
            print(f"  ⏭  {name} already running ({spec['purpose']})")
            already.append(name)
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

    return started, already, failed


def _step_infra() -> StepResult:
    _print_header("Step 3 of 6 — Infrastructure (Docker containers)")
    if not _docker_available():
        return StepResult(
            "infra",
            "skipped",
            "docker daemon unreachable. Install Docker Desktop "
            "(https://docker.com/desktop) and start it.",
        )

    started, skipped, failed = _bring_up_containers(_DOCKER_CONTAINERS)

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
    """Resolve the Ollama model the installer pulls.

    Delegates to ``resolve_llm_model`` (the SINGLE source of truth in
    ``apecx_integration.agents._llm_config``) so the installer pulls
    exactly the model the synthesis runtime later asks for. Before this
    delegation the installer defaulted to ``mistral-nemo:latest`` while
    ``build_chat_llm`` asked for ``nemotron-3-nano:4b`` — a fresh install
    pulled one model and synthesis 404'd on the other. Override both at
    once via the ``APECX_LLM_MODEL`` env var.

    NOTE: the composer is a SEPARATE tier — ``composer_config.yml``
    declares ``mistral-small:latest`` plus per-role bindings that were
    measured-best for its structured-YAML codegen task. Operators who run
    the composer pull those models explicitly; the installer's single pull
    targets the synthesis default only.
    """
    from apecx_integration.agents._llm_config import resolve_llm_model

    return resolve_llm_model()


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


_RHEA_IMAGE = "apecx-rhea-server"
_RHEA_CONTAINER = "apecx-rhea-server"
_RHEA_HOST_PORT = 3001


def _rhea_mcp_url() -> str:
    """The MCP URL consumers (rhea_adapter / discovery / synthesizer
    ``from_env``) read. Default already correct — confirm/export it."""
    return os.environ.get("RHEA_MCP_URL", "http://localhost:3001/mcp/")


def _compose_rhea_container_env() -> dict[str, str]:
    """Container-side Rhea env, reusing the orchestrator's single-source
    derivation and remapping ``localhost`` → ``host.docker.internal``.

    The orchestrator's ``_compose_rhea_env`` derives every value (DB URL,
    Redis/MinIO endpoints, embedding URL, Parsl backend) from the SAME
    ContainerSpec objects the sidecars are launched from — so there is no
    port/host drift. It composes for the HOST-PROCESS path (``localhost``);
    a worker running INSIDE a container reaches those sidecars on the host
    via ``host.docker.internal`` instead, so we remap. Ollama (the
    embedding backend) likewise lives on the host.
    """
    from apecx_integration.infrastructure.orchestrator import _compose_rhea_env

    env = _compose_rhea_env(
        postgres=APECX_RHEA_POSTGRES,
        redis_c=APECX_REDIS,
        minio=APECX_RHEA_MINIO,
        # Ollama lives on the host; the container reaches it via the
        # host gateway. _compose_rhea_env appends /v1.
        ollama_base_url="http://host.docker.internal:11434",
    )
    # Remap every host-loopback reference to the container→host gateway.
    for key in ("DATABASE_URL", "REDIS_HOST", "AGENT_REDIS_HOST", "MINIO_ENDPOINT"):
        if key in env:
            env[key] = env[key].replace("localhost", "host.docker.internal")
    # Bind on all interfaces inside the container (also baked into the
    # image, set here for belt-and-suspenders + host-process parity).
    env["HOST"] = "0.0.0.0"
    # Use the image's baked, writable per-tool conda envs dir — NOT the
    # host-path the orchestrator composes for the host-process backend.
    env["RHEA_CONDA_ENVS_DIR"] = "/opt/rhea-conda/envs"
    return env


def _call_find_tools(mcp_url: str, query: str) -> int:
    """Call ``find_tools(query)`` on the worker over MCP; return the tool
    count it surfaces. The real CC-1 ingest check — a non-empty catalog
    means the ingestion produced rows the RAG can retrieve."""
    import asyncio

    from nanobrain.library.tools._mcp_transport import MCPTransport

    async def _run() -> int:
        transport = MCPTransport(
            mcp_url=mcp_url, timeout_seconds=30.0, client_name="apecx-setup-rhea"
        )
        try:
            # find_tools populates the session catalog as a side effect; we
            # read the surfaced set from the follow-up tools/list below.
            await transport.call(
                "tools/call", {"name": "find_tools", "arguments": {"query": query}}
            )
            # tools/call result: {"content": [...]} where the structured
            # content carries the MCPTool list. Count via the populated
            # tools/list delta instead — find_tools populates the session
            # catalog, so a follow-up tools/list reflects the surfaced set.
            listed = await transport.call("tools/list", {})
            names = [t.get("name") for t in listed.get("tools", [])]
            # Exclude the always-present find_tools entry itself.
            return len([n for n in names if n and n != "find_tools"])
        finally:
            await transport.aclose()

    return asyncio.run(_run())


def _step_rhea() -> StepResult:
    """One-time Rhea bring-up via Docker (E3-4.2/4.3, 2026-06-13).

    Idempotent. Safe to re-run. NEVER raises — degrades to ``skipped``
    when docker is down so the install chain continues.

    Phases:
      1. Locate the Rhea checkout (``_find_rhea_repo`` — same probe the
         orchestrator + autodiscovery use).
      2. Ensure the 3 sidecars (Postgres 5435 / Redis / MinIO) — REUSES
         the orchestrator's ContainerSpecs; never re-declares them.
      3. Ensure mxbai-embed-large is pulled in Ollama (the find_tools RAG
         embedding backend, via the host Ollama).
      4. ``docker build apecx-rhea-server`` from the fork checkout (skip
         when the image is already present unless ``APECX_RHEA_REBUILD=1``).
      5. Run the worker via nanobrain ``DockerMCPWorker.ensure_running()``
         (container→host sidecars; HOST=0.0.0.0; PARSL backend ``local``;
         health-checked by a real MCP handshake).
      6. Ingest the catalog via ``docker exec … update_tools``
         (``RHEA_INGEST_ONLY``, default ``muscle``).
      7. Confirm ``find_tools(query)`` surfaces ≥1 tool (CC-1 non-empty
         ingested catalog) + confirm ``RHEA_MCP_URL``.

    Zero operator env vars required end-to-end.
    """
    _print_header("Step 5b of 6 — Rhea (Docker MCP worker, opt-in)")

    if not _docker_available():
        return StepResult(
            "rhea",
            "skipped",
            "docker daemon unreachable — Rhea worker not brought up. "
            "Install Docker Desktop (https://docker.com/desktop), start it, "
            "then re-run `apecx-setup rhea`. (Chain continues without Rhea.)",
        )

    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        _find_rhea_repo,
    )

    rhea_repo = _find_rhea_repo()
    if rhea_repo is None:
        return StepResult(
            "rhea",
            "skipped",
            "no rhea checkout found in standard locations; "
            "git clone https://github.com/AlexandrNP/rhea.git into the "
            "workspace next to apecx-mcp-integration/ to enable",
        )
    print(f"  ▶  found rhea checkout at {rhea_repo}")

    # Phase 2: sidecars — reuse the SAME three specs infra/orchestrator use.
    print("  ▶  ensuring sidecars (Postgres/Redis/MinIO) ...")
    _started, _already, sidecar_failed = _bring_up_containers(_DOCKER_CONTAINERS)
    if sidecar_failed:
        return StepResult(
            "rhea",
            "fail",
            f"sidecars failed to start: {sidecar_failed}; "
            "run `apecx-setup infra` and inspect `docker logs`",
        )

    # Phase 3: ensure mxbai-embed-large (find_tools RAG embedding backend).
    ollama_binary = shutil.which("ollama")
    if ollama_binary is not None:
        listed = subprocess.run([ollama_binary, "list"], capture_output=True, text=True, timeout=30)
        if "mxbai-embed-large" not in listed.stdout:
            print("  ▶  pulling mxbai-embed-large (~700 MB) ...")
            pull = subprocess.run([ollama_binary, "pull", "mxbai-embed-large"], timeout=900)
            if pull.returncode != 0:
                return StepResult(
                    "rhea",
                    "partial",
                    f"`ollama pull mxbai-embed-large` exited {pull.returncode}; "
                    "find_tools ingestion will fail until you pull it manually",
                )
        else:
            print("  ▶  mxbai-embed-large already present in Ollama")
    else:
        print(
            "  ▶  ollama not on PATH — embedding pull skipped (operator must pull mxbai-embed-large)"
        )

    # Phase 4: build the worker image from the fork (idempotent).
    image_present = (
        subprocess.run(
            ["docker", "image", "inspect", _RHEA_IMAGE],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        == 0
    )
    if not image_present or os.environ.get("APECX_RHEA_REBUILD") == "1":
        print(f"  ▶  docker build {_RHEA_IMAGE} from {rhea_repo} (first build ~2-3 min) ...")
        build = subprocess.run(
            ["docker", "build", "-t", _RHEA_IMAGE, str(rhea_repo)],
            timeout=1800,
        )
        if build.returncode != 0:
            return StepResult(
                "rhea",
                "fail",
                f"`docker build {_RHEA_IMAGE}` exited {build.returncode}; "
                "inspect the build output above",
            )
    else:
        print(f"  ▶  image {_RHEA_IMAGE} already present (APECX_RHEA_REBUILD=1 to rebuild)")

    # Phase 5: run the worker via nanobrain's DockerMCPWorker.
    mcp_url = _rhea_mcp_url()
    worker_env = _compose_rhea_container_env()
    try:
        import asyncio

        from nanobrain.library.runtime.mcp_worker import DockerMCPWorker

        worker = DockerMCPWorker(
            image=_RHEA_IMAGE,
            container_name=_RHEA_CONTAINER,
            mcp_url=mcp_url,
            host_port=_RHEA_HOST_PORT,
            env=worker_env,
            # host.docker.internal resolves automatically on Docker Desktop;
            # --add-host makes it work on native-Linux daemons too.
            extra_run_args=["--add-host", "host.docker.internal:host-gateway"],
            health_timeout_seconds=180.0,
        )
        print("  ▶  starting + health-checking the Rhea worker ...")
        asyncio.run(worker.ensure_running())
    except Exception as exc:  # noqa: BLE001 — never raise from a setup step
        return StepResult(
            "rhea",
            "fail",
            f"Rhea worker did not come up: {type(exc).__name__}: {str(exc)[:300]}",
        )

    # Phase 6: ingest the catalog INSIDE the worker (zero host venv).
    ingest_only = os.environ.get("RHEA_INGEST_ONLY", "muscle")
    print(f"  ▶  ingesting catalog (RHEA_INGEST_ONLY={ingest_only}; ~10s) ...")
    ingest = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"RHEA_INGEST_ONLY={ingest_only}",
            _RHEA_CONTAINER,
            "uv",
            "run",
            "-m",
            "rhea.preprocess.update_tools",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if ingest.returncode != 0:
        return StepResult(
            "rhea",
            "partial",
            f"ingest (`update_tools`) exited {ingest.returncode}: "
            f"{(ingest.stderr or ingest.stdout)[-300:]}",
        )

    # Phase 7: confirm a non-empty ingested catalog (CC-1) + RHEA_MCP_URL.
    # Query by the ingested tool's own name (CC-1's `find_tools("muscle")`)
    # — a semantic query like "sequence alignment" can rank generic Galaxy
    # text tools above a freshly-ingested tool when the catalog is mixed.
    query = ingest_only.split(",")[0].strip() or "muscle"
    try:
        n_tools = _call_find_tools(mcp_url, query)
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            "rhea",
            "partial",
            f"worker up + ingest ran, but find_tools({query!r}) failed: "
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    if n_tools < 1:
        return StepResult(
            "rhea",
            "partial",
            f"worker up + ingest ran, but find_tools({query!r}) surfaced 0 tools "
            f"— catalog may be empty (check `docker logs {_RHEA_CONTAINER}`)",
        )
    os.environ.setdefault("RHEA_MCP_URL", mcp_url)
    return StepResult(
        "rhea",
        "ok",
        f"worker {_RHEA_CONTAINER} healthy at {mcp_url}; "
        f"find_tools({query!r}) surfaced {n_tools} tool(s); "
        f"RHEA_MCP_URL={mcp_url}",
    )


# ---------------------------------------------------------------------------
# Step 5c — PyMOL image (E3-7)
# ---------------------------------------------------------------------------

_PYMOL_IMAGE = "apecx-pymol:3.1.0"


def _step_pymol() -> StepResult:
    """Build the version-pinned headless PyMOL image (E3-7, 2026-06-13).

    The structural-reasoning stage (``StructuralReasoningStep``) shells
    out to ``apecx-pymol:3.1.0`` for real per-residue SASA. Without the
    image the stage degrades to a named-skip; building it here makes the
    real structural path run out of the box.

    Idempotent: skips when the image already exists (unless
    ``APECX_PYMOL_REBUILD=1``). NEVER raises — degrades to ``skipped``
    when docker is down.
    """
    _print_header("Step 5c of 6 — PyMOL image (structural reasoning)")

    if not _docker_available():
        return StepResult(
            "pymol",
            "skipped",
            "docker daemon unreachable — PyMOL image not built. Install "
            "Docker Desktop (https://docker.com/desktop), start it, then "
            "re-run `apecx-setup pymol`. (Chain continues without it.)",
        )

    image_present = (
        subprocess.run(
            ["docker", "image", "inspect", _PYMOL_IMAGE],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        == 0
    )
    if image_present and os.environ.get("APECX_PYMOL_REBUILD") != "1":
        return StepResult(
            "pymol",
            "ok",
            f"image {_PYMOL_IMAGE} already present (APECX_PYMOL_REBUILD=1 to rebuild)",
        )

    # docker/pymol/ lives at the apecx repo root. This module is at
    # src/apecx_integration/cli/setup.py → parents[3] is the repo root.
    repo_root = Path(__file__).resolve().parents[3]
    pymol_ctx = repo_root / "docker" / "pymol"
    dockerfile = pymol_ctx / "Dockerfile"
    if not dockerfile.is_file():
        return StepResult(
            "pymol",
            "fail",
            f"PyMOL Dockerfile not found at {dockerfile}",
        )

    print(f"  ▶  docker build {_PYMOL_IMAGE} from {pymol_ctx} (~5 min, conda solve) ...")
    build = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            _PYMOL_IMAGE,
            "-f",
            str(dockerfile),
            str(pymol_ctx),
        ],
        timeout=1800,
    )
    if build.returncode != 0:
        return StepResult(
            "pymol",
            "fail",
            f"`docker build {_PYMOL_IMAGE}` exited {build.returncode}; "
            "inspect the build output above",
        )
    return StepResult(
        "pymol",
        "ok",
        f"built {_PYMOL_IMAGE} (headless open-source PyMOL, version-pinned)",
    )


# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------


def _step_verify() -> StepResult:
    _print_header("Step 6 of 6 — Verification")
    workspace_root = Path(__file__).resolve().parents[4]
    checks: list[tuple[str, bool, str]] = []

    # Data
    default_data = _setup_data._DEFAULT_DATA_DIR
    data_present = (default_data / "violin" / "Vaccine_Information.csv").exists()
    checks.append(
        (
            "data",
            data_present,
            f"VIOLIN data at {default_data}"
            if data_present
            else "missing — run `apecx-setup data`",
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

    # Rhea (E3-4): the Docker path. We check the static state
    # `apecx-setup rhea` produces: the from-fork worker image built +
    # (optionally) the worker container running. We don't drive an MCP
    # round-trip here (cheap stat-only checks only).
    if not _docker_available():
        rhea_ok = False
        rhea_detail = "docker down — `apecx-setup rhea` (opt-in) needs Docker"
    else:
        image_present = (
            subprocess.run(
                ["docker", "image", "inspect", _RHEA_IMAGE],
                capture_output=True,
                text=True,
                timeout=30,
            ).returncode
            == 0
        )
        if not image_present:
            rhea_ok = False
            rhea_detail = f"worker image {_RHEA_IMAGE} not built — `apecx-setup rhea`"
        elif _container_running(_RHEA_CONTAINER):
            rhea_ok = True
            rhea_detail = f"worker {_RHEA_CONTAINER} running ({_rhea_mcp_url()})"
        else:
            rhea_ok = True
            rhea_detail = f"image {_RHEA_IMAGE} built (worker not currently running)"
    checks.append(("rhea", rhea_ok, rhea_detail))

    # PyMOL (E3-7): the version-pinned structural-reasoning image.
    if not _docker_available():
        pymol_ok = False
        pymol_detail = "docker down — `apecx-setup pymol` needs Docker"
    else:
        pymol_present = (
            subprocess.run(
                ["docker", "image", "inspect", _PYMOL_IMAGE],
                capture_output=True,
                text=True,
                timeout=30,
            ).returncode
            == 0
        )
        pymol_ok = pymol_present
        pymol_detail = (
            f"image {_PYMOL_IMAGE} built"
            if pymol_present
            else f"image {_PYMOL_IMAGE} not built — `apecx-setup pymol`"
        )
    checks.append(("pymol", pymol_ok, pymol_detail))

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
    optional = {"postgres", "redis", "minio", "faiss", "rhea", "pymol"}
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
    "pymol": lambda **_: _step_pymol(),
    "verify": lambda **_: _step_verify(),
}


def _run_all(
    *,
    interactive: bool = True,
    with_rag: bool = False,
    with_rhea: bool = False,
    with_pymol: bool = False,
    prefer_gh_release: bool = False,
) -> int:
    """Run the canonical install chain.

    Chain (G81 + G82 + G84, 2026-05-16):
      1. globus  — preflight: SDK + credentials + endpoint UUIDs.
                   ``skipped`` when not configured (operator gets
                   actionable instructions); ``ok`` enables Globus
                   transfer in the data step.
      2. data    — VIOLIN/BV-BRC CSVs (preferred path: Globus when
                   globus step said OK; fallback: ``gh release download``)
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
    results.append(_step_data(interactive=interactive, prefer_gh_release=prefer_gh_release))
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
    if with_pymol:
        results.append(_step_pymol())
    else:
        results.append(
            StepResult(
                "pymol",
                "skipped",
                "opt-in — run `apecx-setup pymol` or `apecx-setup --with-pymol` to build the headless PyMOL image for the structural-reasoning stage (~5 min one-time)",
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
        choices=["globus", "data", "infra", "llm", "rag", "rhea", "pymol", "verify", "all"],
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
    parser.add_argument(
        "--with-pymol",
        action="store_true",
        help=(
            "Include the headless PyMOL image build (E3-7: ~5 min one-time, "
            "version-pinned apecx-pymol:3.1.0) in the default chain. Run this "
            "when you want the structural-reasoning stage's REAL per-residue "
            "SASA path (otherwise that stage degrades to a named-skip)."
        ),
    )
    parser.add_argument(
        "--prefer-gh-release",
        action="store_true",
        help=(
            "Skip the Globus-first transfer attempt and use the "
            "``gh release download`` path immediately (G82: Globus-first "
            "since 2026-05-16). Useful when Globus IS configured but the "
            "operator wants the same path that pre-G82 installs took, "
            "e.g. for reproducing an older install verbatim."
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
                with_pymol=args.with_pymol,
                prefer_gh_release=args.prefer_gh_release,
            )
        )
    elif args.subcommand == "data":
        result = _step_data(
            interactive=not args.non_interactive,
            prefer_gh_release=args.prefer_gh_release,
        )
    else:
        result = _SUBCOMMANDS[args.subcommand](
            interactive=not args.non_interactive,
        )

    sys.exit(_print_summary([result]))


if __name__ == "__main__":  # pragma: no cover
    main()
