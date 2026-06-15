"""Unit tests for the ``apecx_capabilities`` MCP aggregator tool.

The tool is a CONVENIENCE LAYER over two existing surfaces — it runs no probes
of its own:
  - ``discovery._load_runnable_catalog`` (catalog + ``check_prerequisites``) →
    which workflows are runnable now vs. need configuration, with the honest
    fallback hint.
  - the infrastructure orchestrator's ``status()`` → the backend roster.

Pinned guarantees (2026-06-15):
  1. The two LLM ROLES are stated explicitly and not conflated (desktop/MCP
     frontier LLM vs. backend/headless apecx LLM).
  2. A locked Docker/Rhea workflow surfaces its ``fallback`` hint (the MAFFT /
     LLM-only alternative) — never just a bare "not met".
  3. The runnable/locked split comes straight from ``check_prerequisites``,
     so the tool reflects the SAME availability ``list_workflows`` reports.
"""

from __future__ import annotations

import asyncio

from apecx_integration.mcp_surface.tools.discovery import _load_runnable_catalog
from apecx_integration.mcp_surface.tools.eo_primitives import apecx_capabilities


def test_discovery_rows_carry_unavailable_hint_when_locked():
    rows, err = _load_runnable_catalog()
    assert err is None, err
    locked = [r for r in rows if not r["available"]]
    # Every locked Rhea/Docker workflow declares a fallback hint pointing at the
    # MAFFT or LLM-only path (the catalog declares unavailable_hint on them).
    rhea_locked = [r for r in locked if "rhea" in r["name"] or "muscle" in r["name"]]
    if rhea_locked:  # only when Rhea isn't configured in this env (the usual case)
        assert any(r["unavailable_hint"] for r in rhea_locked), rhea_locked
    # available rows carry an empty hint (no noise).
    for r in rows:
        if r["available"]:
            assert r["unavailable_hint"] == ""


def test_capabilities_states_both_llm_modes_without_conflation():
    caps = asyncio.run(apecx_capabilities())
    modes = caps["modes"]
    assert "desktop_mcp" in modes and "backend_headless" in modes
    # Desktop mode: the MCP client is the synthesizing LLM; no apecx endpoint needed.
    assert "no apecx-side LLM endpoint is required" in modes["desktop_mcp"]
    # Backend mode: names the apecx LLM backend + that there is no remote default.
    assert "APECX_LLM_BASE_URL" in modes["backend_headless"]
    assert "no remote default" in modes["backend_headless"]


def test_capabilities_split_matches_check_prerequisites():
    caps = asyncio.run(apecx_capabilities())
    rows, _ = _load_runnable_catalog()
    n_runnable = sum(1 for r in rows if r["available"])
    assert len(caps["runnable_now"]) == n_runnable
    assert len(caps["needs_configuration"]) == len(rows) - n_runnable
    # Locked entries surface missing prerequisites + a fallback field.
    for entry in caps["needs_configuration"]:
        assert "missing_prerequisites" in entry
        assert "fallback" in entry
    assert "runnable now" in caps["summary"]


def test_capabilities_backends_present_or_errored_loud():
    # The backend roster comes from the orchestrator; at unit time it is not
    # started, so we assert the field is present and either a status snapshot
    # (has 'overall') or a loud error dict — never silently absent.
    caps = asyncio.run(apecx_capabilities())
    backends = caps["backends"]
    assert isinstance(backends, dict)
    assert "overall" in backends or "error" in backends
