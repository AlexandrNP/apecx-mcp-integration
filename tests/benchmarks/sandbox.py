"""Subprocess sandbox for executing benchmark candidates.

Why subprocess and not ``exec()``:

- ``exec`` in the test process leaks state across problems. One
  candidate can monkey-patch ``sys.modules`` and ruin the next.
- ``exec`` doesn't give us a hard timeout signal — Python can't
  preempt a tight loop in pure C extensions.
- Subprocess gives free cleanup (process dies) and a real
  reliability signal — segfaults, OOMs, etc. surface as nonzero
  exit codes instead of contaminating the harness.

The sandbox is NOT a security sandbox. It runs the candidate as
the harness user, with full filesystem and network access. We
rely on (a) trusting the LLM not to spawn ``rm -rf /`` (no obvious
attack motive) and (b) running the harness on a workstation, not
prod. The existing ``apecx_integration.composition.docker_sandbox``
exists for the real-isolation case; wire it in if we ever start
benchmarking adversarial models.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxResult:
    """What a sandbox run returns.

    ``passed`` is True iff the subprocess exited with code 0.
    Stdout/stderr captured for failure diagnostics.

    ``timed_out`` is True iff we hit the wall-clock cap. We
    surface this as a distinct outcome from "exit nonzero" so
    the scorer can bucket timeouts into their own failure class.
    """

    passed: bool
    timed_out: bool
    stdout: str
    stderr: str
    exit_code: int | None


def run_in_subprocess(
    *,
    candidate_code: str,
    setup_code: str,
    test_code: str,
    timeout_seconds: float = 30.0,
    extra_env: dict[str, str] | None = None,
) -> SandboxResult:
    """Execute ``setup_code + candidate_code + test_code`` in a fresh
    Python interpreter and report the outcome.

    Composition order matters: setup runs first (e.g., to define
    helpers the candidate references), then the candidate
    (defining the entry point), then the test (asserts).

    The test_code is wrapped so that any uncaught exception causes
    the subprocess to exit nonzero — pytest-style ``assert``
    failures, ``Exception`` raises, etc. all read as "fail".
    """
    script = (
        "import sys, traceback\n"
        "try:\n"
        f"{_indent(setup_code, 4)}\n"
        f"{_indent(candidate_code, 4)}\n"
        f"{_indent(test_code, 4)}\n"
        "except BaseException:\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    env: dict[str, str] | None = None
    if extra_env:
        # Inherit + override.
        import os

        env = dict(os.environ)
        env.update(extra_env)

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # ``exc.stdout``/``exc.stderr`` may be bytes; coerce.
        return SandboxResult(
            passed=False,
            timed_out=True,
            stdout=_to_str(exc.stdout),
            stderr=_to_str(exc.stderr),
            exit_code=None,
        )

    return SandboxResult(
        passed=(completed.returncode == 0),
        timed_out=False,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
    )


def _indent(text: str, n_spaces: int) -> str:
    """Indent each line of text by ``n_spaces``. Empty input → an
    empty pass statement so the surrounding try block is valid."""
    if not text or not text.strip():
        return " " * n_spaces + "pass"
    prefix = " " * n_spaces
    return "\n".join(prefix + line for line in text.splitlines())


def _to_str(maybe_bytes) -> str:
    if maybe_bytes is None:
        return ""
    if isinstance(maybe_bytes, bytes):
        return maybe_bytes.decode("utf-8", errors="replace")
    return str(maybe_bytes)


def run_workflow_script_in_subprocess(
    *,
    workflow_yaml_path: Path,
    runner_script: str,
    pythonpath: str,
    timeout_seconds: float = 60.0,
) -> SandboxResult:
    """Run a nanobrain workflow execution script as a subprocess.

    For the nanobrain-native benchmark: the candidate produces a
    workflow YAML; we load it via ``Workflow.from_config`` and
    run it with a fixed harness script. The harness verifies the
    expected output and exits 0/1 accordingly.

    ``runner_script`` is the Python source that loads the workflow
    and runs assertions. It receives ``WORKFLOW_PATH`` via env.
    """
    extra_env = {
        "WORKFLOW_PATH": str(workflow_yaml_path),
        "PYTHONPATH": pythonpath,
    }
    return run_in_subprocess(
        candidate_code="",
        setup_code="",
        test_code=runner_script,
        timeout_seconds=timeout_seconds,
        extra_env=extra_env,
    )


__all__ = ["SandboxResult", "run_in_subprocess", "run_workflow_script_in_subprocess"]
