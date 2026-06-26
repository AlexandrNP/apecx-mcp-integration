"""Unit tests for ``deploy_env_lines`` + the ``--emit-deploy-env`` CLI of ``network_config``.

Phase 3 makes the network config file the SOURCE for the deploy stack's backend host-ports:
``install-server.sh`` runs ``python -m ...network_config --emit-deploy-env`` to generate
``deploy/.env.network``, which docker compose interpolates (``${POSTGRES_HOST_PORT}`` …) and the
host ``run-mcp.sh`` wrapper sources. Contract pinned here:
  * defaults yield exactly the 7 documented lines,
  * the Ollama port drives BOTH ``OLLAMA_HOST_PORT`` and the ``APECX_LLM_BASE_URL`` it appears in,
  * the rhea port drives ``RHEA_HOST_PORT``,
  * the ``--emit-deploy-env <path>`` CLI prints the lines for an on-disk config file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from apecx_integration.mcp_surface.network_config import (
    BackendsConfig,
    NetworkConfig,
    RheaConfig,
    deploy_env_lines,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_config_yields_the_seven_documented_lines():
    lines = deploy_env_lines(NetworkConfig())
    assert lines == [
        "POSTGRES_HOST_PORT=5435",
        "REDIS_HOST_PORT=6379",
        "MINIO_HOST_PORT=9000",
        "MINIO_CONSOLE_HOST_PORT=9001",
        "OLLAMA_HOST_PORT=11434",
        "RHEA_HOST_PORT=3001",
        "APECX_LLM_BASE_URL=http://localhost:11434/v1",
    ]


def test_ollama_port_drives_both_host_port_and_llm_base_url():
    cfg = NetworkConfig(backends=BackendsConfig(ollama_port=12000))
    lines = deploy_env_lines(cfg)
    assert "OLLAMA_HOST_PORT=12000" in lines
    assert "APECX_LLM_BASE_URL=http://localhost:12000/v1" in lines


def test_rhea_port_drives_rhea_host_port():
    cfg = NetworkConfig(rhea=RheaConfig(port=3009))
    assert "RHEA_HOST_PORT=3009" in deploy_env_lines(cfg)


def test_emit_deploy_env_cli_prints_lines_for_a_config_file(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"backends": {"postgres_port": 5999}}))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apecx_integration.mcp_surface.network_config",
            "--emit-deploy-env",
            str(p),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    assert "POSTGRES_HOST_PORT=5999" in result.stdout.splitlines()
