"""Centralized network configuration — the single source for apecx ports + hosts.

Replaces the scattered hardcoded defaults + per-setting env vars (``APECX_MCP_HOST/PORT``,
``APECX_CONTROL_PLANE_URL``) with ONE YAML file. Precedence is **CLI flag > config file >
built-in default** — there is no env layer for these settings (a deployment sets them in the
config file, which ``install-server.sh`` also reads to generate ``deploy/.env`` for the
container backends).

Resolution order for the file:
  1. an explicit path passed to :func:`load_network_config` (e.g. an ``apecx-mcp --config`` flag),
  2. ``$APECX_CONFIG`` if set (the ONE bootstrap pointer — a path, not a port/host),
  3. ``~/.apecx/config.yml`` (the default home, alongside the dictionary / data dirs).
When no file is found, the built-in defaults (the pydantic field defaults) apply — so a fresh
install with no config file still works, on the same ports the file documents.

``extra='forbid'`` on every model makes a typo'd key FAIL LOUD instead of silently using a default
(workspace pydantic rule).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

# The default home for the config file — matches ~/.apecx/{dictionary,data,taxdump}.
DEFAULT_CONFIG_PATH = Path("~/.apecx/config.yml").expanduser()
_CONFIG_PATH_ENV = "APECX_CONFIG"


def _port_field(default: int) -> int:
    return Field(default=default, ge=1, le=65535)


class MCPConfig(BaseModel):
    """The MCP server's HTTP bind (streamable-http / sse transports)."""

    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"  # 0.0.0.0 to accept remote connections (loopback by default)
    port: int = _port_field(8001)  # distinct from the control plane (8000)


class ControlPlaneConfig(BaseModel):
    """The Control Plane (run-store + state backend) the MCP server health-checks + POSTs to."""

    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = _port_field(8000)


class RheaConfig(BaseModel):
    """The Rhea MCP worker (Galaxy bio-tools)."""

    model_config = ConfigDict(extra="forbid")
    host: str = "localhost"
    port: int = _port_field(3001)


class BackendsConfig(BaseModel):
    """Container backend HOST ports for the deploy/ stack. ``install-server.sh`` reads these to
    generate ``deploy/.env`` (docker compose interpolates env, not YAML). The bind host is fixed
    at 127.0.0.1 in the compose by design (unauthenticated backends → loopback only)."""

    model_config = ConfigDict(extra="forbid")
    postgres_port: int = _port_field(5435)
    redis_port: int = _port_field(6379)
    minio_port: int = _port_field(9000)
    minio_console_port: int = _port_field(9001)
    ollama_port: int = _port_field(11434)


class NetworkConfig(BaseModel):
    """Top-level apecx network config. Sections default to their own built-in defaults, so a
    partial config file (only the keys you override) is valid."""

    model_config = ConfigDict(extra="forbid")
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    rhea: RheaConfig = Field(default_factory=RheaConfig)
    backends: BackendsConfig = Field(default_factory=BackendsConfig)

    @property
    def control_plane_url(self) -> str:
        return f"http://{self.control_plane.host}:{self.control_plane.port}"

    @property
    def rhea_mcp_url(self) -> str:
        # Trailing slash matters (avoids a 307 redirect on the streamable-http mount).
        return f"http://{self.rhea.host}:{self.rhea.port}/mcp/"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config-file path: explicit arg > $APECX_CONFIG > ~/.apecx/config.yml."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(_CONFIG_PATH_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH


def load_network_config(path: str | Path | None = None) -> NetworkConfig:
    """Load the network config. Missing file → built-in defaults. A malformed file or an unknown
    key raises (ValidationError / yaml error) — FAIL LOUD, never silently fall back to defaults."""
    p = resolve_config_path(path)
    if not p.exists():
        return NetworkConfig()
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: network config must be a YAML mapping, got {type(data).__name__}")
    return NetworkConfig(**data)


# Process-wide active config (set once in apecx-mcp main(), mirrors set_active_locus). Consumers
# call get_network_config() instead of threading the object through every call site.
_active: NetworkConfig | None = None


def set_network_config(config: NetworkConfig | None) -> None:
    """Set the process-wide network config. ``None`` resets to a lazy default (used by tests)."""
    global _active
    _active = config


def get_network_config() -> NetworkConfig:
    """The process-wide network config; lazy-loads the default file on first use if unset."""
    global _active
    if _active is None:
        _active = load_network_config()
    return _active


def deploy_env_lines(config: NetworkConfig) -> list[str]:
    """The deploy-stack env lines docker compose interpolates, derived from the config.

    ``install-server.sh`` writes these to ``deploy/.env.network`` and passes that file to both
    ``docker compose --env-file`` and the host ``run-mcp.sh`` wrapper, so the config file is the
    SINGLE source for the backend host-ports (compose interpolates env, not YAML)."""
    b = config.backends
    return [
        f"POSTGRES_HOST_PORT={b.postgres_port}",
        f"REDIS_HOST_PORT={b.redis_port}",
        f"MINIO_HOST_PORT={b.minio_port}",
        f"MINIO_CONSOLE_HOST_PORT={b.minio_console_port}",
        f"OLLAMA_HOST_PORT={b.ollama_port}",
        f"RHEA_HOST_PORT={config.rhea.port}",
        f"APECX_LLM_BASE_URL=http://localhost:{b.ollama_port}/v1",
    ]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Emit deploy-stack env from the network config.")
    parser.add_argument(
        "--emit-deploy-env", action="store_true", help="print the deploy/.env.network lines"
    )
    parser.add_argument("path", nargs="?", help="config file path (default: resolve_config_path)")
    args = parser.parse_args()

    if args.emit_deploy_env:
        for line in deploy_env_lines(load_network_config(args.path)):
            print(line)
