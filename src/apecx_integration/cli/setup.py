"""apecx-setup orchestrator (2026-05-09).

Single entry point for the entire APECx deployment recipe:

    pip install apecx-mcp-integration
    apecx-setup           # runs ALL steps idempotently
    apecx-setup data      # only download VIOLIN + BV-BRC data
    apecx-setup infra     # only start Postgres + Redis containers
    apecx-setup llm       # only check/pull the Ollama model
    apecx-setup rag       # only build the FAISS RAG index
    apecx-setup verify    # only run the post-setup verification
    apecx-setup --reconfigure-llm   # change LLM env vars in existing config

Each subcommand is idempotent + safe to re-run. The default
(``apecx-setup``) runs every step in dependency order:
    1. ``data``  — download apecx-data tarball (VIOLIN, BV-BRC, FAISS seed)
    2. ``infra`` — start Postgres + Redis containers if Docker is available
    3. ``llm``   — install Ollama if missing (interactive); start daemon;
                   pull the configured model
    4. ``rag``   — build the FAISS RAG index if absent or older than data
    5. ``verify`` — smoke-check every component reports healthy

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


def _step_data(*, interactive: bool = True) -> StepResult:
    """Delegate to existing setup_data._run_full_setup."""
    _print_header("Step 1 of 5 — Data")
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
    try:
        _setup_data._run_full_setup()
    except SystemExit as exc:
        if exc.code == 0:
            return StepResult("data", "ok", "downloaded + extracted")
        return StepResult(
            "data",
            "fail",
            f"setup_data exited with code {exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        return StepResult("data", "fail", f"{type(exc).__name__}: {exc}")
    return StepResult("data", "ok", "downloaded + extracted")


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
    _print_header("Step 2 of 5 — Infrastructure (Docker containers)")
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
    _print_header("Step 3 of 5 — LLM (Ollama install + check + model pull)")

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
    _print_header("Step 4 of 5 — RAG index (FAISS)")
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
# Step 5 — verify
# ---------------------------------------------------------------------------


def _step_verify() -> StepResult:
    _print_header("Step 5 of 5 — Verification")
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
    # that honestly in the partial-vs-fail distinction.
    optional = {"postgres", "redis", "minio"}
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
    "infra": lambda **_: _step_infra(),
    "llm": _step_llm,
    "rag": lambda **_: _step_rag(),
    "verify": lambda **_: _step_verify(),
}


def _run_all(*, interactive: bool = True) -> int:
    print("apecx-setup — orchestrated stack initializer")
    print()

    results: list[StepResult] = []
    results.append(_step_data(interactive=interactive))
    results.append(_step_infra())
    results.append(_step_llm(interactive=interactive))
    results.append(_step_rag())
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
        choices=["data", "infra", "llm", "rag", "verify", "all"],
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
    args = parser.parse_args(argv)

    if args.reconfigure_llm:
        _setup_data._run_reconfigure_llm()
        return

    if args.subcommand in (None, "all"):
        sys.exit(_run_all(interactive=not args.non_interactive))
    elif args.subcommand == "data":
        result = _step_data(interactive=not args.non_interactive)
    else:
        result = _SUBCOMMANDS[args.subcommand](
            interactive=not args.non_interactive,
        )

    sys.exit(_print_summary([result]))


if __name__ == "__main__":  # pragma: no cover
    main()
