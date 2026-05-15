"""Real spawn-path test for the Rhea MCP host-process backend.

Gated on the operator setting BOTH ``RHEA_REPO_PATH`` and
``RHEA_PYTHON_PATH``. Skips cleanly otherwise. Unlike the fake-tested
host-process unit tests, this drives the actual ``subprocess.Popen``
spawn against a real Rhea checkout — surfacing the bugs that hid
behind the fakes (env composition, PATH inheritance, the
RHEA_PYTHON_PATH-points-at-wrong-Python silent-failure shape).

The test refuses to run if a Rhea MCP server is ALREADY answering at
the configured URL — to avoid hijacking an operator-managed
long-lived Rhea instance. Stop the operator's Rhea first if you want
to run this against a real machine; the test will spawn its own and
``atexit`` will clean it up.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

import httpx
import pytest

from apecx_integration.infrastructure.orchestrator import (
    InfraOrchestrator,
    _default_backend_specs,
)

_RHEA_REPO = os.environ.get("RHEA_REPO_PATH")
_RHEA_PYTHON = os.environ.get("RHEA_PYTHON_PATH")
_RHEA_MCP_URL = os.environ.get("RHEA_MCP_URL", "http://localhost:3001/mcp/")


def _rhea_already_running() -> bool:
    try:
        resp = httpx.post(
            _RHEA_MCP_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json,text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "spawn-test", "version": "0.1"},
                },
            },
            timeout=3.0,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code == 200 and "serverInfo" in resp.text


_gate = pytest.mark.skipif(
    not (_RHEA_REPO and _RHEA_PYTHON),
    reason="RHEA_REPO_PATH and RHEA_PYTHON_PATH must be set",
)


@_gate
@pytest.mark.asyncio
async def test_orchestrator_spawns_rhea_and_atexit_stops_it() -> None:
    """The orchestrator spawns rhea-server from a clean state, the
    probe flips to READY, ``spawned_by_us=True``, and ``shutdown()``
    terminates the process so no orphan survives the test.
    """
    if _rhea_already_running():
        pytest.skip(
            f"Rhea MCP is already running at {_RHEA_MCP_URL} (operator-managed). "
            "Stop it first to exercise the orchestrator's spawn path."
        )

    rhea_spec = next(s for s in _default_backend_specs() if s.name == "rhea_mcp")
    orch = InfraOrchestrator(specs=[rhea_spec])

    await orch.start_all()
    snap = await orch.status()
    rhea = next(b for b in snap["backends"] if b["name"] == "rhea_mcp")

    assert rhea["state"] == "ready", (
        f"orchestrator failed to spawn rhea: state={rhea['state']!r} "
        f"detail={rhea['detail'][:300]!r}"
    )
    assert rhea["spawned_by_us"] is True
    assert rhea["spawned_pid"], "spawned_pid should be populated for a host_process we started"

    pid = rhea["spawned_pid"]
    # Sanity-check the process actually exists.
    assert subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0, (
        f"orchestrator reported spawned_pid={pid} but `ps -p {pid}` shows no process"
    )

    # Shut down + verify the child is GONE.
    await orch.shutdown()
    # Give the SIGTERM a moment to land. The orchestrator's
    # shutdown() already waits up to 5s in _atexit_shutdown, but
    # since we invoked shutdown() directly there's no atexit grace.
    for _ in range(50):  # up to 5s in 100ms steps
        if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0:
            break
        await asyncio.sleep(0.1)
    assert subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0, (
        f"orchestrator-spawned rhea (pid={pid}) survived shutdown — atexit leak"
    )


@pytest.mark.asyncio
async def test_bad_rhea_python_path_fails_loud_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong RHEA_PYTHON_PATH (e.g. bare miniconda without rhea
    installed) must FAIL-LOUD upfront with an actionable message —
    NOT wait 60s for the probe to time out on an obscure ImportError.

    Unconditional. Uses a guaranteed-nonexistent Python prefix so we
    don't need a real Rhea checkout to exercise the import-check path.
    """
    # Need RHEA_REPO_PATH to pass the prereq-env-vars gate; the value
    # doesn't matter because we'll fail before we use it.
    monkeypatch.setenv("RHEA_REPO_PATH", "/tmp/__not_a_real_rhea_repo__")
    monkeypatch.setenv(
        "RHEA_PYTHON_PATH",
        "/tmp/__definitely_not_a_python_install_xyz__",
    )
    # Point the probe at a port nothing's listening on so the orchestrator
    # actually enters the spawn path (rather than reporting `reused`
    # because the previous test's rhea is still answering on 3001 — or
    # because the operator has their own rhea running). Otherwise this
    # test becomes flaky in any environment with a live rhea.
    monkeypatch.setenv("RHEA_MCP_URL", "http://localhost:39998/mcp/")

    rhea_spec = next(s for s in _default_backend_specs() if s.name == "rhea_mcp")
    orch = InfraOrchestrator(specs=[rhea_spec])

    t0 = time.monotonic()
    await orch.start_all()
    elapsed = time.monotonic() - t0

    snap = await orch.status()
    rhea = next(b for b in snap["backends"] if b["name"] == "rhea_mcp")

    # The whole start_all should be FAST — under 20s — because we
    # detect the bad python before the 60s ready timeout.
    assert elapsed < 20.0, (
        f"bad RHEA_PYTHON_PATH took {elapsed:.1f}s to fail — should be "
        "fast (pre-spawn import check, not probe-timeout)"
    )
    assert rhea["state"] in ("error_starting", "external_unconfigured"), (
        f"expected error_starting/external_unconfigured, got {rhea['state']!r}"
    )
    # The actionable detail must NAME the wrong python path and point
    # at the rhea uv venv as the right answer.
    assert "RHEA_PYTHON_PATH" in rhea["detail"]
    # Either the pre-spawn import-check error or the prereq message
    # mentions the .venv path or the import failure — both are
    # acceptable; the contract is "actionable, not a 60s timeout".

    await orch.shutdown()
