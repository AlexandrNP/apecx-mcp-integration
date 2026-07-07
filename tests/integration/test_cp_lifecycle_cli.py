"""Phase 0 integration: the `apecx-cp` CLI serve/stop/restart wiring, end to end.

Spawns the REAL `apecx-cp serve` in a subprocess against an isolated home + sqlite DB on a
free port, then asserts the wiring: a 2nd plain serve refuses (exit 1, no traceback), restart
yields a NEW pid, and stop frees the port + removes the pid file. Locks the app.py glue that
the unit tests (which cover only the _serve_lifecycle primitives) can't.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(home: Path) -> dict:
    import os

    e = dict(os.environ)
    e["APECX_CP_HOME"] = str(home)
    e["APECX_CP_DB_URL"] = f"sqlite:///{home / 'cp.db'}"
    e["PYTHONPATH"] = "src"
    return e


def _run(env, *args, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "apecx_integration.control_plane.app", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_port(port, up=True, timeout=45.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            listening = s.connect_ex(("127.0.0.1", port)) == 0
        if listening == up:
            return True
        time.sleep(0.2)
    return False


@pytest.mark.integration
def test_serve_refuses_restart_and_stop_cli(tmp_path):
    home = tmp_path
    home.mkdir(exist_ok=True)
    env = _env(home)
    port = _free_port()
    pidf = home / "cp.pid"
    procs = []
    try:
        p1 = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apecx_integration.control_plane.app",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p1)
        assert _wait_port(port, up=True), "server1 never started listening"
        pid1 = int(pidf.read_text().splitlines()[0])

        # A 2nd plain serve must REFUSE (exit 1) with a message, not an errno-48 traceback.
        r = _run(env, "serve", "--port", str(port))
        assert r.returncode == 1, r.stdout + r.stderr
        assert "already in use" in r.stdout
        assert "Traceback" not in (r.stdout + r.stderr)

        # restart -> a NEW pid bound on the same port; old pid gone.
        p2 = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apecx_integration.control_plane.app",
                "restart",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p2)
        deadline = time.monotonic() + 45
        pid2 = pid1
        while time.monotonic() < deadline:
            if pidf.exists():
                cur = int(pidf.read_text().splitlines()[0])
                if cur != pid1 and _wait_port(port, up=True, timeout=1):
                    pid2 = cur
                    break
            time.sleep(0.2)
        assert pid2 != pid1, "restart did not bind a new pid"

        # stop -> port freed + pid file removed.
        r = _run(env, "stop")
        assert r.returncode == 0
        assert _wait_port(port, up=False), "port not freed after stop"
        assert not pidf.exists()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                with contextlib.suppress(Exception):
                    p.wait(timeout=5)
