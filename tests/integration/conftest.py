"""Shared fixtures for the Control Plane integration test suite.

Each test that talks to the HTTP API gets a fresh migrated SQLite DB
and a FastAPI ``TestClient`` wired to it. No mocks; no in-memory
shortcuts — it's a real engine, real migrations, real handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cp_engine(tmp_path: Path):
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


@pytest.fixture
def cp_client(cp_engine) -> TestClient:
    return TestClient(create_app(engine=cp_engine))
