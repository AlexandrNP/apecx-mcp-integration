"""Per-tool pre-warm helpers — the install + report-shape primitives.

This module ships the low-level building blocks the pre-warm
NANOBRAIN WORKFLOW (at
``apecx_integration.infrastructure.prewarm_workflow``) is built from:

* :class:`PrewarmReport`, :class:`ToolPrewarmResult` — the typed
  shape the orchestrator's :meth:`InfraOrchestrator.status` surfaces
  under ``rhea_tool_prewarm`` and lifts into ``actionable`` on
  failures.
* :func:`prewarm_tool` — single-tool installer (cache probe → fetch
  requirements from rhea Postgres → spawn rhea-venv subprocess →
  install_conda_env with await-pack). Called by
  :class:`InstallToolsStep`.
* :func:`_collect_tools_from_catalog`, :func:`_fetch_tool_requirements`
  — catalog walker + Postgres JSONB unwrap. Called by
  :class:`CollectToolsStep` and :func:`prewarm_tool` respectively.

The imperative ``prewarm_workflow_catalog(...)`` driver this module
used to ship was retired 2026-05-15 in favor of the nanobrain
workflow — see ``infrastructure/prewarm_workflow/configs/prewarm_workflow.yml``
for the single correct entry point.

Why this is its own module + a separate orchestrator phase
----------------------------------------------------------
Rhea's tool execution lives behind an Academy actor whose
``agent_on_startup`` calls ``rhea.agent.utils.install_conda_env`` to
build the tool's conda env on first invocation. Two real reliability
problems flow from that lazy-install design:

1. The first user invocation pays a 30-90 s install cost. Claude
   Desktop's MCP timeouts (or just a user's patience) end before the
   install completes, and they retry — re-triggering the install,
   compounding the latency.
2. If ``install_conda_env`` raises (corrupt conda, channel
   misconfiguration, wrong major version, libarchive crash inside
   conda-libmamba-solver — see ``rhea/agent/utils.py`` for the four
   silent-failure shapes we now refuse), the Academy actor enters a
   "failed startup" state and every subsequent ``run_tool`` returns
   ``"Action 'run_tool' was cancelled by the agent."`` for the rest
   of the rhea-server's lifetime. The operator has no way to recover
   short of restarting rhea-server.

The pre-warm phase sidesteps both problems by calling
``install_conda_env`` DIRECTLY (not through the Academy actor) at
orchestrator startup time. The conda env is built, packed, and
cached in Redis BEFORE any MCP tool can be invoked. The first
real user call hits the Redis cache, the actor's
``agent_on_startup`` unpacks the archive (~1 s, no install), and
there's no wedge risk because the slow + fragile install already
ran in a context where errors propagate cleanly.

This is the same anti-silent-failure pattern as the infra
orchestrator: shift failures to the EARLIEST observable moment with
the LOUDEST report, never let them surface mid-user-workflow as a
cryptic timeout or cancelled-action.

How tools are declared
----------------------
Each ``WorkflowCatalogEntry`` declares its ``prewarm_rhea_tools``:
the list of Rhea-side tool names (matching the ``id`` column of
Rhea's ``galaxytools`` Postgres table) whose envs must be ready
before this workflow can serve a real call. The orchestrator
collects the union across the catalog (deduped) and pre-warms each.

What this module does NOT do
----------------------------
- It does NOT modify Rhea's actor lifecycle. A truly defensive
  Rhea agent would catch ``install_conda_env`` errors in
  ``agent_on_startup`` and surface them per-call instead of wedging
  — but that's a Rhea-side change, separate from this orchestration
  layer.
- It does NOT run the tool. Only the env install. Triggering a
  full ``tools/call`` would be heavy and risk the wedge problem
  the direct-Python-API approach exists to avoid.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ToolPrewarmResult:
    """Per-tool pre-warm outcome — fed to the status tool."""

    tool_name: str
    state: str  # "ready" | "reused" | "failed" | "skipped"
    detail: str = ""
    latency_seconds: float = 0.0
    error: str | None = None


@dataclass
class PrewarmReport:
    """Aggregate report across all pre-warmed tools."""

    tools: list[ToolPrewarmResult] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def all_ready(self) -> bool:
        """True iff every declared tool is ready/reused."""
        return all(r.state in {"ready", "reused"} for r in self.tools)

    def snapshot(self) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": r.tool_name,
                    "state": r.state,
                    "detail": r.detail,
                    "latency_seconds": r.latency_seconds,
                    **({"error": r.error} if r.error else {}),
                }
                for r in self.tools
            ],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "all_ready": self.all_ready,
        }


def _collect_tools_from_catalog(catalog: Any) -> list[str]:
    """Walk ``WorkflowCatalog.workflows`` and dedupe their prewarm lists."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in getattr(catalog, "workflows", []):
        for tool_name in getattr(entry, "prewarm_rhea_tools", []) or []:
            if tool_name not in seen:
                seen.add(tool_name)
                out.append(tool_name)
    return out


async def _fetch_tool_requirements(database_url: str, tool_id: str) -> list[dict]:
    """Query Rhea's ``galaxytools`` table for one tool's requirements.

    Returns a list of raw dicts of the shape
    ``{"type": "package", "value": "<pkg>", "version": "<ver>"}`` —
    NOT Pydantic ``Requirement`` objects. The orchestrator's venv may
    not have ``rhea`` importable; the rhea-venv subprocess that does
    the actual install re-hydrates these into ``Requirement``
    instances on its own side.

    Raises ``RuntimeError`` if the tool isn't in the registry — the
    catalog declared a prereq Rhea doesn't know about, which is a
    config bug the operator must fix.
    """
    import json

    import psycopg

    # The rhea container's Postgres URL uses postgresql+asyncpg://;
    # strip the +asyncpg driver suffix to feed psycopg.
    if database_url.startswith("postgresql+asyncpg://"):
        sync_url = "postgresql://" + database_url[len("postgresql+asyncpg://") :]
    else:
        sync_url = database_url
    async with (
        await psycopg.AsyncConnection.connect(sync_url, connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT definition->'requirements' FROM galaxytools WHERE id = %s",
            (tool_id,),
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(
            f"Rhea tool {tool_id!r} not found in the galaxytools table. "
            f"Run `python -m rhea.preprocess.update_tools` (with "
            f"RHEA_INGEST_ONLY={tool_id} if you want only this one) to "
            f"ingest it before declaring it in prewarm_rhea_tools."
        )
    requirements_json = row[0]
    if isinstance(requirements_json, str):
        requirements_json = json.loads(requirements_json)
    # Rhea stores the column as a nested wrapper:
    #   {"containers": [...], "requirements": [{type, value, version}, ...]}
    # (see rhea/utils/schema.py — `Tool.requirements` is itself a model
    # carrying both fields). Earlier code did `list(requirements_json)`
    # which on a dict yields the top-level KEYS ('containers',
    # 'requirements') — a silent-failure shape that ships the wrong
    # payload to the subprocess. Unwrap explicitly and refuse anything
    # other than the dict-with-requirements shape we know about.
    if isinstance(requirements_json, dict):
        if "requirements" not in requirements_json:
            raise RuntimeError(
                f"Rhea tool {tool_id!r} has an unexpected requirements "
                f"shape: dict without a 'requirements' key (got keys "
                f"{sorted(requirements_json)!r}). The catalog's "
                f"prewarm_rhea_tools entry must match Rhea's "
                f"galaxytools.definition->'requirements' shape."
            )
        requirements_list = requirements_json["requirements"]
    else:
        # Some older Rhea ingests may have stored a bare list directly.
        # Accept it; downstream subprocess re-validates via Requirement.
        requirements_list = requirements_json
    if not isinstance(requirements_list, list):
        raise RuntimeError(
            f"Rhea tool {tool_id!r}: inner 'requirements' is "
            f"{type(requirements_list).__name__}, expected list."
        )
    return list(requirements_list)


_PREWARM_RUNNER_SCRIPT = r'''
"""One-shot pre-warm runner — executed via Rhea's own venv Python.

Reads {tool_id, requirements, redis_host, redis_port} from stdin
(one-line JSON; the requirements list is the orchestrator-fetched
shape of the galaxytools.definition->'requirements' JSONB), calls
rhea.agent.utils.install_conda_env inline. Exits 0 on success,
non-zero on failure with the message in stderr.

Lives as a runtime-emitted script so apecx-mcp's venv does NOT need
rhea's transitive deps (conda_pack, openai-embeddings, parsl, etc.).
The script runs inside Rhea's uv-managed venv where those deps are
already pinned + present. The Postgres lookup is intentionally done
by the orchestrator (which has psycopg) and the result is passed in
— Rhea's venv ships asyncpg+sqlalchemy, not psycopg, so doing the DB
query inside this subprocess would force another dep alignment.
"""
import asyncio, json, sys
data = json.loads(sys.stdin.read())
tool_id = data["tool_id"]
requirements_raw = data["requirements"]
redis_host = data["redis_host"]
redis_port = int(data["redis_port"])

from redis import StrictRedis
from rhea.agent.utils import install_conda_env
from rhea.utils.schema import Requirement

requirements = [Requirement(**r) for r in requirements_raw]

async def main():
    r = StrictRedis(host=redis_host, port=redis_port)
    await install_conda_env(
        env_name=tool_id,
        requirements=requirements,
        r=r,
        target_path=f"/tmp/apecx-rhea-prewarm-{tool_id}",
    )

asyncio.run(main())
print(json.dumps({"status": "ok"}))
'''


async def prewarm_tool(
    tool_id: str,
    *,
    database_url: str,
    redis_host: str,
    redis_port: int,
    rhea_python: str | None = None,
) -> ToolPrewarmResult:
    """Pre-install a single Rhea tool's conda env.

    Reuses the Redis cache if already populated. Otherwise spawns the
    install via Rhea's own venv Python (so the orchestrator's venv
    does NOT have to carry rhea's transitive deps like conda_pack).
    The subprocess calls ``rhea.agent.utils.install_conda_env`` which
    carries the post-install verification chain — strict-pin pre-create
    remove, conda self-heal on recoverable corruption, bin/-content
    check, major-version-skew check.
    """
    t0 = time.monotonic()

    import asyncio as _asyncio
    import json as _json

    import redis as _redis

    r = _redis.StrictRedis(host=redis_host, port=redis_port)
    try:
        already_cached = bool(r.hexists("conda_envs", tool_id))
    except Exception as exc:  # noqa: BLE001
        return ToolPrewarmResult(
            tool_name=tool_id,
            state="failed",
            detail=(f"could not probe Redis for cached conda env: {type(exc).__name__}: {exc}"),
            latency_seconds=time.monotonic() - t0,
            error=str(exc),
        )

    if already_cached:
        return ToolPrewarmResult(
            tool_name=tool_id,
            state="reused",
            detail=f"conda env present in Redis cache (`HEXISTS conda_envs {tool_id}` -> 1)",
            latency_seconds=time.monotonic() - t0,
        )

    # Cache miss — fetch the requirements list from Postgres in the
    # orchestrator's venv (which has psycopg), then ship them as JSON
    # to the rhea-venv subprocess that has conda_pack + the rhea
    # install_conda_env path. Splitting the DB read from the install
    # avoids forcing rhea's venv to grow a psycopg dep just for us.
    try:
        requirements = await _fetch_tool_requirements(database_url, tool_id)
    except Exception as exc:  # noqa: BLE001
        return ToolPrewarmResult(
            tool_name=tool_id,
            state="failed",
            detail=f"could not query rhea Postgres for requirements: {type(exc).__name__}: {exc}",
            latency_seconds=time.monotonic() - t0,
            error=str(exc),
        )
    # Already JSON-safe dicts from psycopg's JSONB → Python decoding.
    requirements_raw = requirements

    # Cache miss — run the install in Rhea's venv.
    if rhea_python is None:
        rhea_python_bin = os.environ.get("RHEA_PYTHON_PATH")
        if not rhea_python_bin:
            return ToolPrewarmResult(
                tool_name=tool_id,
                state="failed",
                detail=(
                    "cache miss and $RHEA_PYTHON_PATH is unset, so the "
                    "orchestrator has no way to spawn the rhea-venv "
                    "subprocess that does the install. Set "
                    "RHEA_PYTHON_PATH=$RHEA_REPO_PATH/.venv/bin or "
                    "skip prewarm by removing the entry from the "
                    "catalog's prewarm_rhea_tools list."
                ),
                latency_seconds=time.monotonic() - t0,
                error="RHEA_PYTHON_PATH unset",
            )
        rhea_python = f"{rhea_python_bin.rstrip('/')}/python"

    payload = _json.dumps(
        {
            "tool_id": tool_id,
            "requirements": requirements_raw,
            "redis_host": redis_host,
            "redis_port": redis_port,
        }
    )
    # The Rhea venv expects `cd $RHEA_REPO_PATH` for its
    # version-resolution + relative-config-file paths to work. Use it
    # if exported, else let the subprocess default to current cwd.
    cwd = os.environ.get("RHEA_REPO_PATH") or None
    # Carry CONDA_EXE through to the subprocess so rhea/agent/utils.py
    # picks up the operator's intended conda (matching the
    # orchestrator's PATH composition). Mirrors the env we set when
    # spawning rhea-server itself.
    sub_env = dict(os.environ)
    sub_env.setdefault("PYTHONUNBUFFERED", "1")
    if "CONDA_EXE" not in sub_env:
        conda_bin = os.environ.get("RHEA_CONDA_BIN")
        if conda_bin:
            sub_env["CONDA_EXE"] = f"{conda_bin.rstrip('/')}/conda"
            sub_env["PATH"] = f"{conda_bin.rstrip('/')}:{sub_env.get('PATH', '')}"

    try:
        proc = await _asyncio.create_subprocess_exec(
            rhea_python,
            "-c",
            _PREWARM_RUNNER_SCRIPT,
            stdin=_asyncio.subprocess.PIPE,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            cwd=cwd,
            env=sub_env,
        )
        stdout, stderr = await proc.communicate(input=payload.encode())
    except Exception as exc:  # noqa: BLE001
        return ToolPrewarmResult(
            tool_name=tool_id,
            state="failed",
            detail=f"could not spawn the rhea-venv prewarm subprocess: {type(exc).__name__}: {exc}",
            latency_seconds=time.monotonic() - t0,
            error=str(exc),
        )

    if proc.returncode != 0:
        # Last 400 chars of stderr — enough to carry the actionable
        # rhea verification message + cap the noise.
        stderr_tail = stderr.decode("utf-8", "replace")[-400:].strip()
        return ToolPrewarmResult(
            tool_name=tool_id,
            state="failed",
            detail=(
                f"install_conda_env raised (rhea-venv subprocess exit "
                f"{proc.returncode}). Tail of stderr: {stderr_tail!r}"
            ),
            latency_seconds=time.monotonic() - t0,
            error=stderr_tail,
        )

    return ToolPrewarmResult(
        tool_name=tool_id,
        state="ready",
        detail=f"conda env built + packed + cached in Redis under key {tool_id!r}",
        latency_seconds=time.monotonic() - t0,
    )


__all__ = [
    "PrewarmReport",
    "ToolPrewarmResult",
    "prewarm_tool",
]
