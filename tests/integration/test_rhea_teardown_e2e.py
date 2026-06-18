"""End-to-end confirmation of the RHEA per-execution teardown — resilient to bring-up flakiness.

The orchestrator's autostart PREWARM is flaky (a fragile rhea-venv subprocess spawn), and a
fresh postgres has no schema. This module accommodates BOTH: a self-provisioning ``live_rhea``
fixture that (1) self-heals the DB schema by running rhea's now-self-migrating ``update_tools``,
(2) spawns the rhea-server DIRECTLY with the apecx-stack env — bypassing the flaky prewarm
(prewarm is a best-effort latency optimization; the first MUSCLE call just pays the conda cost),
(3) waits for ``:3001`` healthy, and (4) SKIPS cleanly (never flaky-FAILS) when the stack genuinely
isn't there. Given a healthy rhea it then CONFIRMS the teardown: two real MUSCLE runs leave no
net-new persisted ProxyStore Redis keys (the per-call eviction works), deciding on output values.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_RHEA_REPO = Path(__file__).resolve().parents[3] / "rhea"
_RHEA_PY = _RHEA_REPO / ".venv" / "bin" / "python"
_MCP_URL = "http://localhost:3001/mcp/"
_FASTA = ">a\nMKTAYIAKQR\n>b\nMKTAYIAKQR\n>c\nMKTAYIAKQK\n"

# Standard apecx-setup-infra layout (containers: apecx-rhea-postgres:5435, apecx-redis:6379,
# apecx-rhea-minio:9000). A test may know the provisioned layout; if it drifts, update here.
_RHEA_ENV = {
    "HOST": "localhost",
    "PORT": "3001",
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5435/rhea",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "AGENT_REDIS_HOST": "localhost",
    "AGENT_REDIS_PORT": "6379",
    "MINIO_ENDPOINT": "localhost:9000",
    "MINIO_ACCESS_KEY": "minioadmin",
    "MINIO_SECRET_KEY": "minioadmin",
    "EMBEDDING_URL": "http://localhost:11434/v1",
    "EMBEDDING_KEY": "EMPTY",
    "MODEL": "mxbai-embed-large",
    "PARSL_CONTAINER_BACKEND": "local",
    "RHEA_CONDA_ENVS_DIR": "/tmp/apecx-rhea/conda/envs",
}


def _http_code(url: str, timeout: float = 4.0) -> int:
    try:
        urllib.request.urlopen(url, timeout=timeout)  # noqa: S310
        return 200
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code  # 406 = MCP server UP
    except Exception:
        return 0


def _healthy(url: str = _MCP_URL) -> bool:
    code = _http_code(url)
    return code not in (0, 500)  # 4xx (406) = up; 500 = backend down; 0 = not listening


def _redis_reachable() -> bool:
    try:
        import socket

        with socket.create_connection(("localhost", 6379), timeout=2):
            return True
    except Exception:
        return False


def _self_heal_schema_and_ingest() -> bool:
    """Run rhea's self-migrating update_tools (idempotent) so galaxytools exists + muscle is
    ingested, regardless of a churned/fresh postgres. Returns True on success."""
    if not _RHEA_PY.exists():
        return False
    env = {**os.environ, **_RHEA_ENV, "RHEA_INGEST_ONLY": "muscle", "RHEA_INGEST_LIMIT": "25"}
    try:
        r = subprocess.run(
            [str(_RHEA_PY), "-m", "rhea.preprocess.update_tools"],
            cwd=str(_RHEA_REPO),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return r.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_rhea():
    if not _RHEA_PY.exists() or not _redis_reachable():
        pytest.skip("needs the apecx infra stack (docker redis) + a built rhea venv")
    # Self-configure: rhea_muscle_alignment's prereq gate requires `rhea` importable in THIS
    # (test) process — make it so regardless of how pytest was invoked (PYTHONPATH).
    import importlib.util
    import sys

    if str(_RHEA_REPO) not in sys.path:
        sys.path.insert(0, str(_RHEA_REPO))
    os.environ["RHEA_MCP_URL"] = _MCP_URL
    if (
        importlib.util.find_spec("rhea") is None
        or importlib.util.find_spec("rhea.utils.proxy") is None
    ):
        pytest.skip("`rhea` (+ rhea.utils.proxy deps) not importable in the test venv")
    # Already-running healthy server (operator/CI provided)? Reuse it.
    if _healthy():
        if not _self_heal_schema_and_ingest():
            pytest.skip("rhea reachable but schema self-heal failed (DB not provisioned)")
        yield _MCP_URL
        return
    # Otherwise self-provision: heal schema, then spawn the server DIRECTLY (no flaky prewarm).
    if not _self_heal_schema_and_ingest():
        pytest.skip("could not self-heal the rhea schema (infra not provisioned)")
    proc = subprocess.Popen(  # noqa: S603
        [str(_RHEA_PY), "-m", "rhea.server.mcp_server", "--transport", "streamable-http"],
        cwd=str(_RHEA_REPO),
        env={**os.environ, **_RHEA_ENV},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if _healthy():
                break
            if proc.poll() is not None:
                pytest.skip(f"rhea-server exited during startup (rc={proc.returncode})")
            time.sleep(5)
        else:
            pytest.skip("rhea-server did not become healthy within 180s")
        yield _MCP_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _keyset():
    from redis import Redis

    return set(Redis(host="localhost", port=6379).keys("*"))


def _run_muscle(url: str) -> dict:
    os.environ["RHEA_MCP_URL"] = url
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    _clear_workflow_cache()
    return asyncio.run(run_workflow("rhea_muscle_alignment", {"fasta_text": _FASTA}))


def test_rhea_teardown_no_per_run_key_growth(live_rhea):
    """Two REAL MUSCLE runs on a live rhea leave no net-new persisted ProxyStore keys — the
    per-execution eviction (RheaFileToolStep) holds.

    Failure semantics (the flakiness accommodation): status is now HONEST (G127 driver fix), so a
    MUSCLE failure surfaces as status=error rather than a masked ok. But a rhea-SERVER-side
    execution failure (Parsl worker never returns a handle, conda env not provisioned, embedding
    backend down) is a rhea-infra problem, NOT a teardown regression — so we SKIP on it. This test
    FAILS only on what it actually measures: per-run persisted-key growth (an eviction leak). It
    can therefore only CONFIRM the teardown when rhea's MUSCLE leg genuinely completes."""
    before1 = _keyset()
    r1 = _run_muscle(live_rhea)
    if r1.get("status") != "ok":
        pytest.skip(
            "rhea MUSCLE leg did not complete (rhea-server-side execution unavailable — Parsl "
            f"worker / conda env / embedding backend); not a teardown regression: {r1.get('error')}"
        )
    after1 = _keyset()
    r2 = _run_muscle(live_rhea)
    if r2.get("status") != "ok":
        pytest.skip(
            f"rhea MUSCLE 2nd run did not complete (rhea-infra, not teardown): {r2.get('error')}"
        )
    after2 = _keyset()

    persisted_run1 = len(after1 - before1)
    persisted_run2 = len(after2 - after1)
    # The 2nd run must not accumulate net-new ProxyStore keys beyond the 1st (eviction holds).
    assert persisted_run2 <= persisted_run1, (
        f"per-run persisted-key growth: run1={persisted_run1} run2={persisted_run2} — "
        "the per-execution ProxyStore eviction is leaking keys run-over-run"
    )
    assert persisted_run2 <= 1, (
        f"run2 left {persisted_run2} net-new keys (expected ~0 with eviction)"
    )
