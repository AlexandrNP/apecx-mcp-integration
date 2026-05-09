"""Unit tests for ``build_docker_sandbox_command`` argv construction.

These tests pin every hardening flag from the T13b threat-model table.
If someone weakens or drops a flag here, the corresponding test goes
red and the reviewer sees it immediately.

These tests run on any machine — no Docker required, no
``APECX_T13B_SANDBOX_EXECUTE`` gate needed. ``build_docker_sandbox_command``
is a pure function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.docker_sandbox import (
    SandboxConfig,
    build_docker_sandbox_command,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair_value(argv: list[str], flag: str) -> str:
    """Return the value following ``flag`` in ``argv`` (``--foo value`` pairs).

    Raises ``AssertionError`` if the flag is missing or is the last arg.
    """
    assert flag in argv, f"{flag!r} not in argv: {argv}"
    idx = argv.index(flag)
    assert idx + 1 < len(argv), f"{flag!r} is the last element in argv"
    return argv[idx + 1]


def _all_pair_values(argv: list[str], flag: str) -> list[str]:
    """Return every value paired with ``flag`` (flag appears multiple times)."""
    values: list[str] = []
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            values.append(argv[i + 1])
    return values


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


def test_argv_starts_with_docker_run():
    argv = build_docker_sandbox_command(
        ["python", "-c", "print('hi')"],
        input_host_path=None,
    )
    assert argv[0] == "docker"
    assert argv[1] == "run"


def test_image_precedes_command():
    """Sanity — image name must come after all flags and before the command.

    Otherwise Docker parses the command as the image.
    """
    cfg = SandboxConfig(image="python:3.12-slim")
    argv = build_docker_sandbox_command(
        ["python", "-c", "print('hi')"],
        input_host_path=None,
        config=cfg,
    )
    img_idx = argv.index("python:3.12-slim")
    # Command follows the image
    assert argv[img_idx + 1 : img_idx + 4] == ["python", "-c", "print('hi')"]


def test_rm_flag_present():
    """``--rm`` ensures the container is removed on exit; no persistent
    layer diff that might hold dropped payloads."""
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert "--rm" in argv


# ---------------------------------------------------------------------------
# Threat-model row 3 — network isolation
# ---------------------------------------------------------------------------


def test_network_none_by_default():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--network") == "none"


def test_network_honors_override():
    """Non-default networks are a weakening — explicit opt-in required.

    This test doesn't recommend overriding; it documents that the
    mechanism exists so Phase-3 integration tests can (e.g.) build
    a custom network with docker network policies.
    """
    cfg = SandboxConfig(network="bridge")
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=None,
        config=cfg,
    )
    assert _pair_value(argv, "--network") == "bridge"


# ---------------------------------------------------------------------------
# Threat-model row 2 — filesystem write (read-only root + bounded tmpfs)
# ---------------------------------------------------------------------------


def test_read_only_root():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert "--read-only" in argv


def test_tmpfs_bounded_and_world_writable():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    val = _pair_value(argv, "--tmpfs")
    assert val.startswith("/tmp:")
    assert "size=" in val
    assert "mode=1777" in val  # world-writable + sticky


def test_tmpfs_size_honors_override():
    cfg = SandboxConfig(tmpfs_size="64m")
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=None,
        config=cfg,
    )
    assert "/tmp:size=64m,mode=1777" in argv


# ---------------------------------------------------------------------------
# Threat-model row 4 — process escape (identity, capabilities, syscalls)
# ---------------------------------------------------------------------------


def test_runs_as_nobody_by_default():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--user") == "65534:65534"


def test_drops_all_capabilities():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--cap-drop") == "ALL"


def test_no_new_privileges():
    """Prevents setuid binaries from gaining privileges at exec time."""
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    security_opts = _all_pair_values(argv, "--security-opt")
    assert "no-new-privileges:true" in security_opts


def test_seccomp_default_profile_applied_implicitly():
    """Docker's default seccomp profile (blocks ~60 syscalls including
    ptrace, mount, unshare, reboot, keyctl) applies automatically when
    no ``--security-opt seccomp=...`` flag is passed. We deliberately
    do NOT pass the flag here because the literal ``seccomp=default``
    is NOT a Docker keyword — Docker Desktop on Mac treats it as a
    file path and the container fails to start. The build_docker_sandbox_command
    must NEVER include ``seccomp=unconfined`` (which would disable the
    default profile)."""
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    security_opts = _all_pair_values(argv, "--security-opt")
    # Must NOT explicitly disable seccomp:
    assert "seccomp=unconfined" not in security_opts
    # Should not name the profile explicitly (Docker handles default
    # automatically; explicit naming is the bug we're guarding against):
    assert not any(opt.startswith("seccomp=") for opt in security_opts), (
        f"Sandbox command leaked an explicit seccomp= flag: "
        f"{[o for o in security_opts if o.startswith('seccomp=')]}. "
        f"Default profile applies automatically; explicit naming "
        f"breaks portability across Docker engines (Desktop on Mac "
        f"vs Docker CE on Linux)."
    )


# ---------------------------------------------------------------------------
# Threat-model row 5 — resource exhaustion
# ---------------------------------------------------------------------------


def test_memory_cap():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--memory") == "512m"


def test_memory_swap_equals_memory():
    """--memory-swap == --memory disables swap entirely. Without this,
    a process that hits the memory cap can keep running indefinitely
    against swap, defeating the cap."""
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--memory") == _pair_value(argv, "--memory-swap")


def test_cpu_cap():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--cpus") == "1.0"


def test_pids_cap():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert _pair_value(argv, "--pids-limit") == "256"


def test_resource_caps_honor_override():
    cfg = SandboxConfig(memory_mb=1024, cpus=2.5, pids_limit=64)
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=None,
        config=cfg,
    )
    assert _pair_value(argv, "--memory") == "1024m"
    assert _pair_value(argv, "--memory-swap") == "1024m"
    assert _pair_value(argv, "--cpus") == "2.5"
    assert _pair_value(argv, "--pids-limit") == "64"


# ---------------------------------------------------------------------------
# Threat-model row 1+2 — input mount is read-only
# ---------------------------------------------------------------------------


def test_no_bind_mount_when_input_is_none():
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert "--mount" not in argv


def test_bind_mount_is_read_only(tmp_path: Path):
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=tmp_path,
    )
    mount_val = _pair_value(argv, "--mount")
    assert mount_val.startswith("type=bind,")
    assert f"source={tmp_path.resolve()}," in mount_val
    assert "target=/work" in mount_val
    assert "readonly" in mount_val.split(",")  # last segment must be readonly


def test_bind_mount_target_honors_workdir_override(tmp_path: Path):
    cfg = SandboxConfig(workdir="/artifact")
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=tmp_path,
        config=cfg,
    )
    assert _pair_value(argv, "--workdir") == "/artifact"
    mount_val = _pair_value(argv, "--mount")
    assert "target=/artifact" in mount_val


# ---------------------------------------------------------------------------
# Container naming — for future kill-on-cancel
# ---------------------------------------------------------------------------


def test_no_name_by_default():
    """Without ``container_name``, Docker auto-generates a name. That's
    fine for one-shot runs; naming matters only when we need to kill
    externally (Phase-3)."""
    argv = build_docker_sandbox_command(["true"], input_host_path=None)
    assert "--name" not in argv


def test_container_name_flag_when_provided():
    argv = build_docker_sandbox_command(
        ["true"],
        input_host_path=None,
        container_name="t13b-test-xyz",
    )
    assert _pair_value(argv, "--name") == "t13b-test-xyz"


# ---------------------------------------------------------------------------
# Escape hatch for explicit caller opt-ins
# ---------------------------------------------------------------------------


def test_extra_run_args_appended_before_image(tmp_path: Path):
    """``extra_run_args`` is the documented opt-in for Phase-3
    additions like ``--runtime=runsc``. They must land between the
    core flag block and the image, not after the command."""
    cfg = SandboxConfig(
        extra_run_args=("--runtime=runsc", "--label", "t13b=phase3"),
    )
    argv = build_docker_sandbox_command(
        ["python", "-V"],
        input_host_path=None,
        config=cfg,
    )
    runsc_idx = argv.index("--runtime=runsc")
    img_idx = argv.index(cfg.image)
    assert runsc_idx < img_idx
    # Label is a pair, confirm it is intact
    assert argv[argv.index("--label") + 1] == "t13b=phase3"
