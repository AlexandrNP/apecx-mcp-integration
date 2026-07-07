"""Serve-process lifecycle — PID file + stale-bind detection + stop.

Why this exists: `apecx-cp teardown` only manages the DB infra (Postgres container);
in the default ``sqlite_no_infra`` mode it never touched the uvicorn process, so an old
`apecx-cp serve` kept :8000 bound and the next `serve` died with "address already in
use". These helpers let `serve`/`stop`/`restart` manage the HTTP server itself. The pid
file lives next to ``cp.db`` (``db.cp_home_dir()``) so both agree on the home dir.

The pid file records ``pid`` AND the ``host:port`` it bound, so ``stop`` can apply a
PID-reuse guard: it only signals a live pid that is ALSO the one holding its recorded
port. A recycled pid (an unrelated process that inherited the number after a crash) is
not listening on our port, so it is left alone. Residual (accepted): if our server
crashed, its pid was recycled, AND some other process coincidentally rebound the exact
host:port, the guard can't tell them apart — negligible on a single-user machine.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import socket
import time
from pathlib import Path

from apecx_integration.control_plane.db import cp_home_dir


def pid_file() -> Path:
    return cp_home_dir() / "cp.pid"


def _alive(pid: int) -> bool:
    """True iff a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user — still "alive"
    return True


def _read_record() -> tuple[int, str, int] | None:
    """Parse the pid file into ``(pid, host, port)``; None if absent/malformed."""
    try:
        lines = pid_file().read_text().splitlines()
        pid = int(lines[0].strip())
        host, _, port = lines[1].strip().partition(":")
        return pid, host, int(port)
    except (FileNotFoundError, IndexError, ValueError):
        return None


def read_running_pid() -> int | None:
    """The live server's pid from the pid file, or None if absent/stale.

    A stale pid file (process no longer alive) is removed so a fresh `serve` isn't
    blocked by a leftover from a crash.
    """
    rec = _read_record()
    if rec is None:
        return None
    pid = rec[0]
    if _alive(pid):
        return pid
    pid_file().unlink(missing_ok=True)
    return None


def write_pid(host: str, port: int) -> None:
    """Atomically record this process + the address it is serving (tmp + os.replace)."""
    p = pid_file()
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(f"{os.getpid()}\n{host}:{port}\n")
    os.replace(tmp, p)


def remove_pid() -> None:
    pid_file().unlink(missing_ok=True)


def port_in_use(host: str, port: int) -> bool:
    """True iff something is already LISTENING on ``host:port``.

    SO_REUSEADDR matches uvicorn's own bind, so a port merely in TIME_WAIT (no live
    listener) is reported free — avoiding a spurious "already in use" refusal. This is a
    friendly preflight, not a lock; a TOCTOU race with the real uvicorn bind is acceptable.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as exc:
            return exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL)
        return False


def stop_running(*, timeout: float = 10.0) -> int | None:
    """Stop the pid-file server: SIGTERM, wait up to ``timeout``, then SIGKILL.

    Returns the stopped pid, or None if nothing was running. PID-reuse guard: only
    signal a live pid that is ALSO holding its recorded port. Always clears the pid file.
    """
    rec = _read_record()
    if rec is None:
        return None
    pid, host, port = rec
    if not _alive(pid) or not port_in_use(host, port):
        # Dead, or a recycled pid not listening on our port — don't signal it; just clean up.
        remove_pid()
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid()
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    remove_pid()
    return pid


def wait_port_free(host: str, port: int, *, timeout: float = 10.0) -> bool:
    """Poll until ``host:port`` is free (post-stop) or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_in_use(host, port):
            return True
        time.sleep(0.1)
    return not port_in_use(host, port)
