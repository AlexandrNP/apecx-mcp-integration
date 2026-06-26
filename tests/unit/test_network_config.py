"""Unit tests for the centralized network config loader (``mcp_surface.network_config``).

The loader replaces the scattered per-setting env vars (``APECX_MCP_HOST/PORT``,
``APECX_CONTROL_PLANE_URL``) with ONE YAML file. Contract pinned here:
  * missing file → built-in defaults (a fresh install with no config still works),
  * a partial override file overrides ONLY the keys it names (sections default individually),
  * a typo'd / unknown key FAILS LOUD (``extra='forbid'``), never silently uses a default,
  * an out-of-range port FAILS LOUD (range-validated 1..65535),
  * a non-mapping top-level FAILS LOUD,
  * the ``control_plane_url`` / ``rhea_mcp_url`` properties format correctly,
  * ``set_network_config`` / ``get_network_config`` round-trip the process-wide active config.

These are pure-config tests (no external dependency to mock); the file is read off a
tmp_path fixture. The active-config global is reset in teardown so process state does
not leak into other tests.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from apecx_integration.mcp_surface.network_config import (
    BackendsConfig,
    ControlPlaneConfig,
    MCPConfig,
    NetworkConfig,
    RheaConfig,
    get_network_config,
    load_network_config,
    set_network_config,
)


@pytest.fixture(autouse=True)
def _reset_active_config():
    """Never leak the process-wide active config into another test."""
    yield
    set_network_config(None)


# ---------------------------------------------------------------------------
# load_network_config
# ---------------------------------------------------------------------------


def test_missing_file_returns_defaults():
    cfg = load_network_config("/nonexistent/path/to/config.yml")
    assert isinstance(cfg, NetworkConfig)
    assert cfg.mcp == MCPConfig()
    assert cfg.mcp.host == "127.0.0.1"
    assert cfg.mcp.port == 8001
    assert cfg.control_plane.port == 8000
    assert cfg.rhea.port == 3001
    assert cfg.backends == BackendsConfig()


def test_partial_override_only_touches_named_key(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"mcp": {"port": 9001}}))
    cfg = load_network_config(p)
    assert cfg.mcp.port == 9001
    # the un-named sibling key keeps its default
    assert cfg.mcp.host == "127.0.0.1"
    # un-named sections keep their defaults
    assert cfg.control_plane == ControlPlaneConfig()
    assert cfg.rhea == RheaConfig()


def test_unknown_key_raises(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"mcp": {"prot": 9001}}))  # typo: prot
    with pytest.raises(ValidationError):
        load_network_config(p)


def test_out_of_range_port_raises(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump({"mcp": {"port": 70000}}))
    with pytest.raises(ValidationError):
        load_network_config(p)


def test_non_mapping_top_level_raises(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump([1, 2, 3]))  # a list, not a mapping
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_network_config(p)


def test_empty_file_returns_defaults(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("")  # yaml.safe_load -> None -> {} -> defaults
    cfg = load_network_config(p)
    assert cfg == NetworkConfig()


# ---------------------------------------------------------------------------
# URL properties
# ---------------------------------------------------------------------------


def test_control_plane_url_format():
    cfg = NetworkConfig(control_plane=ControlPlaneConfig(host="prod-cp.internal", port=8000))
    assert cfg.control_plane_url == "http://prod-cp.internal:8000"


def test_default_control_plane_url():
    assert NetworkConfig().control_plane_url == "http://127.0.0.1:8000"


def test_rhea_mcp_url_format():
    cfg = NetworkConfig(rhea=RheaConfig(host="localhost", port=3001))
    # trailing slash matters (avoids a 307 redirect on the streamable-http mount)
    assert cfg.rhea_mcp_url == "http://localhost:3001/mcp/"


# ---------------------------------------------------------------------------
# set_network_config / get_network_config round-trip
# ---------------------------------------------------------------------------


def test_set_get_round_trip():
    cfg = NetworkConfig(mcp=MCPConfig(host="0.0.0.0", port=9001))
    set_network_config(cfg)
    assert get_network_config() is cfg
    assert get_network_config().mcp.port == 9001


def test_set_none_resets_to_lazy_default():
    sentinel = NetworkConfig(mcp=MCPConfig(port=9001))
    set_network_config(sentinel)
    assert get_network_config() is sentinel
    set_network_config(None)
    # lazy-reloads from disk on next access — a fresh object, NOT the sentinel we set. (We don't
    # assert a port value: the lazy load reads ~/.apecx/config.yml if present on this host.)
    fresh = get_network_config()
    assert isinstance(fresh, NetworkConfig)
    assert fresh is not sentinel
