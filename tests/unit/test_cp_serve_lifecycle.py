"""Phase 0: apecx-cp serve-process lifecycle (PID file + stale-bind + stop).

These pin the primitives that let `apecx-cp stop`/`restart` manage the uvicorn server
that `teardown` (sqlite_no_infra) never touched — the gap that stranded :8000. Real
sockets + a real spawned subprocess; no mocks of the OS interface. The PID-reuse guard
means `stop_running` only signals a live pid that is ALSO holding its recorded port, so
the stop test spawns a real listener.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from apecx_integration.control_plane import _serve_lifecycle as life


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the pid file at a temp home so tests never touch a real ~/.apecx-cp/cp.pid."""
    monkeypatch.setenv("APECX_CP_HOME", str(tmp_path))
    yield


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pid_roundtrip_and_stale_detection():
    assert life.read_running_pid() is None  # no file yet
    life.write_pid("127.0.0.1", _free_port())  # records THIS (alive) process
    assert life.read_running_pid() is not None
    # A record naming a not-alive pid is stale -> None + file removed.
    life.pid_file().write_text("2147480000\n127.0.0.1:8000\n")
    assert life.read_running_pid() is None
    assert not life.pid_file().exists()


def test_port_in_use_detects_a_real_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        host, port = srv.getsockname()
        assert life.port_in_use(host, port) is True
    assert life.port_in_use("127.0.0.1", port) is False  # freed after close


def test_stop_running_returns_none_when_nothing_running():
    assert life.stop_running() is None


def test_stop_running_ignores_a_recycled_pid_not_on_its_port():
    """PID-reuse guard: a live pid that is NOT listening on the recorded port is left alone."""
    port = _free_port()  # nobody listening here
    # This test process is alive but not bound to `port` — mimics a recycled pid.
    life.pid_file().write_text(f"{__import__('os').getpid()}\n127.0.0.1:{port}\n")
    assert life.stop_running() is None  # did NOT signal us
    assert not life.pid_file().exists()  # but cleaned the stale record


def test_stop_running_terminates_a_real_listener():
    port = _free_port()
    code = (
        "import socket,time;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));s.listen(1);time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(50):  # wait until it is actually listening
            if life.port_in_use("127.0.0.1", port):
                break
            time.sleep(0.1)
        life.pid_file().write_text(f"{proc.pid}\n127.0.0.1:{port}\n")
        assert life.stop_running(timeout=10.0) == proc.pid
        assert proc.poll() is not None or proc.wait(timeout=5) is not None
        assert not life.pid_file().exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
