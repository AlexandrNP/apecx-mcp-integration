"""#8 (2026-07-01) — apecx-setup's docker-run argv binds internal backends to LOOPBACK.

Mirror of the ``container_run_args`` loopback pin in ``test_infrastructure_orchestrator.py``: the
CLI ``_spec_to_run_args`` (used to build ``_DOCKER_CONTAINERS`` that ``apecx-setup infra`` iterates)
must publish ``127.0.0.1:H:C``, not ``0.0.0.0`` — an unauthenticated Postgres/Redis/MinIO must not be
world-visible. The two argv builders are deliberately kept in sync.
"""

from __future__ import annotations

from apecx_integration.cli.setup import _DOCKER_CONTAINERS, _spec_to_run_args
from apecx_integration.infrastructure.containers import (
    APECX_REDIS,
    APECX_RHEA_MINIO,
    APECX_RHEA_POSTGRES,
)


def _published(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "-p"]


def test_spec_to_run_args_binds_loopback_by_default():
    for spec in (APECX_RHEA_POSTGRES, APECX_REDIS, APECX_RHEA_MINIO):
        pub = _published(_spec_to_run_args(spec))
        assert pub, f"{spec.container_name} publishes no ports"
        assert all(m.startswith("127.0.0.1:") for m in pub), pub
        assert not any(m.startswith("0.0.0.0:") for m in pub)


def test_spec_to_run_args_override_exposes_world():
    pub = _published(_spec_to_run_args(APECX_REDIS, bind_host="0.0.0.0"))
    assert pub and all(m.startswith("0.0.0.0:") for m in pub)


def test_docker_containers_registry_all_publish_loopback():
    # The actual container registry apecx-setup iterates — no entry may bind 0.0.0.0.
    for entry in _DOCKER_CONTAINERS:
        for m in _published(entry["args"]):
            assert m.startswith("127.0.0.1:"), f"{entry['name']} exposes {m!r} to the world"
