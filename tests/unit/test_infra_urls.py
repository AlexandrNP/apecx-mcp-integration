"""Unit tests for infra URL classification (decide_infra_mode)."""

from __future__ import annotations

import pytest
from apecx_integration.control_plane.infra.urls import (
    InfraMode,
    decide_infra_mode,
)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./cp.db",
        "sqlite:///:memory:",
        "sqlite:////abs/path/cp.db",
    ],
)
def test_sqlite_variants_are_no_infra(url: str) -> None:
    d = decide_infra_mode(url)
    assert d.mode is InfraMode.SQLITE_NO_INFRA
    assert d.local_port is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp",
        "postgresql://apecx:apecx@127.0.0.1:5433/apecx_cp",
        "postgres://apecx:apecx@[::1]:5433/apecx_cp",
    ],
)
def test_loopback_on_managed_port_is_local(url: str) -> None:
    d = decide_infra_mode(url)
    assert d.mode is InfraMode.LOCAL_POSTGRES_MANAGED
    assert d.local_port == 5433


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://apecx:apecx@db.internal:5432/apecx_cp",
        "postgresql://alex@pg.corp.example:6543/scratch",
    ],
)
def test_remote_host_is_byo(url: str) -> None:
    d = decide_infra_mode(url)
    assert d.mode is InfraMode.REMOTE_POSTGRES_BYO


def test_loopback_wrong_port_is_byo() -> None:
    d = decide_infra_mode("postgresql+psycopg://apecx:apecx@localhost:5432/apecx_cp")
    assert d.mode is InfraMode.REMOTE_POSTGRES_BYO
    assert "5432" in d.reason


def test_unknown_scheme_is_byo() -> None:
    d = decide_infra_mode("mysql://user@localhost/x")
    assert d.mode is InfraMode.REMOTE_POSTGRES_BYO
    assert "mysql" in d.reason.lower()
