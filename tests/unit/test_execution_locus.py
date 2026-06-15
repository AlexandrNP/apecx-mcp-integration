"""execution_locus — the desktop/agent flag: resolution, fail-loud, server wiring, CLI flag."""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    resolve_locus,
    set_active_locus,
)


@pytest.fixture(autouse=True)
def _restore_locus():
    prior = get_active_locus()
    yield
    set_active_locus(prior)


def test_default_is_desktop(monkeypatch):
    monkeypatch.delenv("APECX_EXECUTION_LOCUS", raising=False)
    assert resolve_locus() == ExecutionLocus.DESKTOP
    assert resolve_locus(None) == ExecutionLocus.DESKTOP


def test_explicit_values_resolve():
    assert resolve_locus("desktop") == ExecutionLocus.DESKTOP
    assert resolve_locus("agent") == ExecutionLocus.AGENT
    assert resolve_locus("AGENT") == ExecutionLocus.AGENT  # case-insensitive


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("APECX_EXECUTION_LOCUS", "agent")
    assert resolve_locus() == ExecutionLocus.AGENT
    # An explicit value (the --locus flag) WINS over the env fallback.
    assert resolve_locus("desktop") == ExecutionLocus.DESKTOP


def test_unknown_value_fails_loud():
    with pytest.raises(ValueError, match="Invalid execution locus"):
        resolve_locus("backend")  # a plausible typo must NOT silently default to desktop


def test_reexport_shim_is_the_same_objects():
    from apecx_integration.mcp_surface import locus as shim

    assert shim.ExecutionLocus is ExecutionLocus
    assert shim.resolve_locus is resolve_locus


def test_build_server_sets_active_locus_and_records_it():
    from apecx_integration.mcp_surface.server import build_server

    srv = build_server(locus=ExecutionLocus.AGENT)
    assert get_active_locus() == ExecutionLocus.AGENT
    assert srv.execution_locus == ExecutionLocus.AGENT

    srv2 = build_server(locus=ExecutionLocus.DESKTOP)
    assert get_active_locus() == ExecutionLocus.DESKTOP
    assert srv2.execution_locus == ExecutionLocus.DESKTOP


def test_server_exposes_same_tool_surface_in_both_loci():
    # The locus steers synthesis, NOT the tool surface — both faces expose the same tools.
    from apecx_integration.mcp_surface.server import build_server

    desktop = {t.name for t in asyncio.run(build_server(locus=ExecutionLocus.DESKTOP).list_tools())}
    agent = {t.name for t in asyncio.run(build_server(locus=ExecutionLocus.AGENT).list_tools())}
    assert desktop == agent


def test_cli_locus_flag_parses():
    from apecx_integration.mcp_surface.server import _build_arg_parser

    assert _build_arg_parser().parse_args(["--locus", "agent"]).locus == "agent"
    assert _build_arg_parser().parse_args([]).locus is None  # unset → resolve_locus defaults
    with pytest.raises(SystemExit):  # argparse rejects an invalid choice
        _build_arg_parser().parse_args(["--locus", "nope"])
