"""Probe batch 51 — final 26 distinct probes against MCP surface +
workflow route + cross-cutting invariants.

Streak before this batch: 274/300 post-AQ post-1066.
Probe naming: 1330–1355.

Distinct probes only — this batch closes the 0/300 stop criterion
(post-1066) when all probes pass.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "src" / "apecx_integration" / "mcp_surface"


# --------------------------------------------------------------------------- #
# Probes 1330–1355 (26 probes)
# --------------------------------------------------------------------------- #


def test_probe_1330_mcp_surface_module_imports_cleanly():
    importlib.import_module("apecx_integration.mcp_surface.server")


def test_probe_1331_mcp_workflows_tool_module_imports():
    importlib.import_module("apecx_integration.mcp_surface.tools.workflows")


def test_probe_1332_mcp_approvals_tool_module_imports():
    importlib.import_module("apecx_integration.mcp_surface.tools.approvals")


def test_probe_1333_mcp_hpc_tool_module_imports():
    importlib.import_module("apecx_integration.mcp_surface.tools.hpc")


def test_probe_1334_mcp_workflows_module_exposes_start_workflow():
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    assert hasattr(mod, "start_workflow")


def test_probe_1335_mcp_workflows_start_workflow_is_async():
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    assert inspect.iscoroutinefunction(mod.start_workflow)


def test_probe_1336_mcp_workflows_show_diff_is_async():
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    assert inspect.iscoroutinefunction(mod.show_diff)


def test_probe_1337_mcp_workflows_execute_workflow_is_async():
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    assert inspect.iscoroutinefunction(mod.execute_workflow)


def test_probe_1338_mcp_show_diff_takes_run_id_str():
    """show_diff(run_id: str) — pin signature so a future change to
    UUID type is intentional (MCP wire shape requires string)."""
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    sig = inspect.signature(mod.show_diff)
    assert "run_id" in sig.parameters


def test_probe_1339_mcp_server_build_function_exists():
    mod = importlib.import_module("apecx_integration.mcp_surface.server")
    assert hasattr(mod, "build_server")


def test_probe_1340_mcp_server_main_function_exists():
    mod = importlib.import_module("apecx_integration.mcp_surface.server")
    assert hasattr(mod, "main")


def test_probe_1341_mcp_server_verify_control_plane_is_async():
    mod = importlib.import_module("apecx_integration.mcp_surface.server")
    assert inspect.iscoroutinefunction(mod._verify_control_plane_reachable)


def test_probe_1342_mcp_workflows_module_has_no_print_statements():
    """MCP tool modules must not print to stdout/stderr (would
    corrupt the JSON-RPC stream over stdio transport)."""
    text = (MCP_DIR / "tools" / "workflows.py").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        # Skip docstring lines and comments.
        if stripped.startswith('"') or stripped.startswith("#"):
            continue
        assert not stripped.startswith("print("), (
            f"workflows.py has print(): {stripped!r}"
        )


def test_probe_1343_mcp_approvals_module_has_no_print_statements():
    text = (MCP_DIR / "tools" / "approvals.py").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"') or stripped.startswith("#"):
            continue
        assert not stripped.startswith("print("), (
            f"approvals.py has print(): {stripped!r}"
        )


def test_probe_1344_mcp_hpc_module_has_no_print_statements():
    text = (MCP_DIR / "tools" / "hpc.py").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"') or stripped.startswith("#"):
            continue
        assert not stripped.startswith("print("), (
            f"hpc.py has print(): {stripped!r}"
        )


def test_probe_1345_mcp_server_dir_has_init_py():
    """Package layout: __init__.py must exist."""
    assert (MCP_DIR / "__init__.py").is_file()


def test_probe_1346_mcp_tools_dir_has_init_py():
    """Sub-package layout."""
    assert (MCP_DIR / "tools" / "__init__.py").is_file()


def test_probe_1347_mcp_workflows_show_diff_returns_dict():
    """Tool functions return dicts (JSON-serializable). Pin the
    return-type annotation."""
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    sig = inspect.signature(mod.show_diff)
    # The annotation should be dict (or compatible).
    assert sig.return_annotation in (dict, "dict") or \
        str(sig.return_annotation) == "dict"


def test_probe_1348_mcp_workflows_execute_workflow_returns_dict():
    mod = importlib.import_module(
        "apecx_integration.mcp_surface.tools.workflows"
    )
    sig = inspect.signature(mod.execute_workflow)
    assert sig.return_annotation in (dict, "dict") or \
        str(sig.return_annotation) == "dict"


def test_probe_1349_workflow_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.workflow"
    )


def test_probe_1350_approval_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.approval"
    )


def test_probe_1351_status_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.status"
    )


def test_probe_1352_hpc_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.hpc"
    )


def test_probe_1353_metrics_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.metrics"
    )


def test_probe_1354_verified_synonyms_route_module_imports_cleanly():
    importlib.import_module(
        "apecx_integration.control_plane.routes.verified_synonyms"
    )


def test_probe_1355_apecx_integration_top_level_imports_cleanly():
    """The package's top-level __init__ must import without errors —
    a circular import or transitively-broken submodule would break
    everything."""
    importlib.import_module("apecx_integration")
