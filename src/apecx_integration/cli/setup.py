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
    3. ``llm``   — pull the configured Ollama model if missing
    4. ``rag``   — build the FAISS RAG index if absent or older than data
    5. ``verify`` — smoke-check every component reports healthy

Brutal-truth design notes:

- Every step gracefully degrades when the underlying optional
  capability is absent (no Docker, no Ollama, no gh-CLI). The exit
  code captures whether the FULL setup succeeded — partial-success
  is reported via a summary table at the end.

- We DO NOT install Docker, Ollama, or gh ourselves — those need
  the user's package manager / system installer. The setup tells
  the user EXACTLY what's missing and how to install it.

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

_DOCKER_CONTAINERS = [
    {
        "name": "apecx-postgres",
        "image": "postgres:16",
        "args": ["-p", "5432:5432", "-e", "POSTGRES_PASSWORD=apecx"],
        "ready_check": ["pg_isready", "-U", "postgres", "-h", "localhost"],
        "purpose": "G21 PostgresTaskStore + future durable surfaces",
    },
    {
        "name": "apecx-redis",
        "image": "redis:7",
        "args": ["-p", "6379:6379"],
        "ready_check": ["redis-cli", "ping"],
        "purpose": "G5 Step 2 ProxyStore Redis backend + Academy exchange",
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
    return os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _step_llm() -> StepResult:
    _print_header("Step 3 of 5 — LLM (Ollama model pull)")
    if shutil.which("ollama") is None:
        return StepResult(
            "llm",
            "skipped",
            "`ollama` CLI not found. Install from https://ollama.com/download "
            "(or set APECX_LLM_BASE_URL to a remote OpenAI-compatible endpoint).",
        )

    # Check daemon is responsive
    import urllib.error
    import urllib.request

    api_url = _ollama_url() + "/api/tags"
    try:
        with urllib.request.urlopen(api_url, timeout=5) as resp:
            tags = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return StepResult(
            "llm",
            "skipped",
            f"ollama daemon unreachable at {api_url}. Start with: ollama serve",
        )

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

    # Postgres
    pg_running = _docker_available() and _container_running("apecx-postgres")
    checks.append(
        (
            "postgres",
            pg_running,
            "container apecx-postgres responsive"
            if pg_running
            else "not running — `apecx-setup infra` (or skip if not using PostgresTaskStore)",
        )
    )

    # Redis
    redis_running = _docker_available() and _container_running("apecx-redis")
    checks.append(
        (
            "redis",
            redis_running,
            "container apecx-redis responsive"
            if redis_running
            else "not running — `apecx-setup infra` (or skip if not using Redis backend)",
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
    # Postgres + Redis are optional for many workflows; reflect that
    # honestly in the partial-vs-fail distinction.
    optional = {"postgres", "redis"}
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

_SUBCOMMANDS: dict[str, Callable[[], StepResult]] = {
    "infra": _step_infra,
    "llm": _step_llm,
    "rag": _step_rag,
    "verify": _step_verify,
}


def _run_all(*, interactive: bool = True) -> int:
    print("apecx-setup — orchestrated stack initializer")
    print()

    results: list[StepResult] = []
    results.append(_step_data(interactive=interactive))
    results.append(_step_infra())
    results.append(_step_llm())
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
        result = _SUBCOMMANDS[args.subcommand]()

    sys.exit(_print_summary([result]))


if __name__ == "__main__":  # pragma: no cover
    main()
