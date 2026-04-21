"""Unit tests for ApptainerRuntime command construction.

These don't invoke apptainer. They pin down the exact argv we'd issue
so that if the command shape drifts silently (e.g., a refactor accidentally
drops the --bind flag, or the teardown loses the managed-subdir safety),
CI catches it without needing an HPC machine.

End-to-end verification belongs in tests/integration/test_apptainer_runtime.py,
which runs only when apptainer is reachable (including macOS-via-Lima).
"""

from __future__ import annotations

from apecx_integration.control_plane.infra.apptainer_runtime import (
    DATA_SUBDIR,
    INSTANCE_NAME,
    ApptainerRuntime,
)
from apecx_integration.control_plane.infra.runtime import PostgresConfig


def _cfg(tmp_path_str: str = "/tmp/apecx_cp_data") -> PostgresConfig:
    return PostgresConfig(data_dir=tmp_path_str)


def test_ensure_commands_pull_docker_image_and_bind_managed_subdir() -> None:
    """The runtime binds a subdirectory (``apecx_cp_postgres/``) under
    the caller's ``data_dir``, never the caller's directory itself.
    Destructive teardown is thereby bounded to that subdir.
    """
    runtime = ApptainerRuntime(binary="apptainer")
    cmds = runtime.build_ensure_commands(_cfg("/home/alex/.apecx_cp/pg"))
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd[0] == "apptainer"
    assert cmd[1:4] == ["instance", "start", "--bind"]
    assert cmd[4] == f"/home/alex/.apecx_cp/pg/{DATA_SUBDIR}:/var/lib/postgresql/data"
    assert "docker://postgres:16-alpine" in cmd
    assert cmd[-1] == INSTANCE_NAME


def test_ensure_commands_inject_postgres_env_and_pgdata() -> None:
    runtime = ApptainerRuntime(binary="apptainer")
    cmd = runtime.build_ensure_commands(_cfg())[0]
    joined = " ".join(cmd)
    assert "POSTGRES_USER=apecx" in joined
    assert "POSTGRES_PASSWORD=apecx" in joined
    assert "POSTGRES_DB=apecx_cp" in joined
    # PGDATA must be under the bind-mounted data dir on Postgres 16
    # Alpine; otherwise the container refuses to initdb on an existing
    # non-empty mount point.
    assert "PGDATA=/var/lib/postgresql/data/pgdata" in joined


def test_singularity_binary_is_accepted_as_alias() -> None:
    """On older HPC deployments the binary is still named singularity."""
    runtime = ApptainerRuntime(binary="singularity")
    cmd = runtime.build_ensure_commands(_cfg())[0]
    assert cmd[0] == "singularity"


def test_teardown_without_remove_data_just_stops_instance() -> None:
    runtime = ApptainerRuntime(binary="apptainer")
    cmds = runtime.build_teardown_commands(_cfg("/tmp/pg"), remove_data=False)
    assert cmds == [["apptainer", "instance", "stop", INSTANCE_NAME]]


def test_teardown_with_remove_data_rm_rfs_only_the_managed_subdir() -> None:
    """Safety: ``rm -rf`` must target the managed subdirectory, not the
    caller's ``data_dir`` parent. If a user passes ``data_dir=/home/alex``,
    we must never rm -rf /home/alex.
    """
    runtime = ApptainerRuntime(binary="apptainer")
    cmds = runtime.build_teardown_commands(_cfg("/home/alex"), remove_data=True)
    assert cmds == [
        ["apptainer", "instance", "stop", INSTANCE_NAME],
        ["rm", "-rf", f"/home/alex/{DATA_SUBDIR}"],
    ]


def test_is_running_command_uses_json_listing() -> None:
    runtime = ApptainerRuntime(binary="apptainer")
    assert runtime.build_is_running_command() == [
        "apptainer",
        "instance",
        "list",
        "--json",
    ]
