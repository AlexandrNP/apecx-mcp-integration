"""INVARIANT: every PRODUCT workflow runs in BOTH execution modes — desktop and headless.

The architecture promises two execution modes for every workflow (design: the locus inversion).
A workflow satisfies this iff it does NOT require a server-side LLM in DESKTOP locus, i.e. it is
either:
  - DETERMINISTIC (no LLM step), or
  - FINAL-SYNTHESIS-ONLY (its single LLM step is ``LLM_ROLE='final_synthesis'`` and omits its call
    in desktop, letting the host synthesize).

A workflow with an IN-DAG LLM step CANNOT be desktop-inverted: the host (Claude Desktop) has no
MCP sampling (decision D2), so the server cannot call back to it mid-run — that step needs a
server LLM in BOTH modes. Such a workflow is *agent-shaped* and must not be offered as a desktop
``product`` workflow.

This test pins the guarantee: it loads every discovered ``product`` workflow and asserts it is
desktop-clean. When someone adds a new in-DAG-LLM product workflow, this FAILS LOUD — the
remedy is to make it final-synthesis-only, mark it ``LLM_ROLE='none'`` on a deterministic step
mis-flagged by the heuristic, or categorize it non-product (demo/benchmark) / retire it.

KNOWN EXCEPTION — ``violin_bvbrc``: the pre-harmonized-search workflow bakes entity extraction +
LLM synonym proposals INTO the DAG (real in-DAG LLM). It is superseded by ``harmonized_search``
(deterministic resolution) + ``viral_epitope_evidence_review`` (final-synthesis), and is slated
for retirement (a ~dozen-file change: discovered workflow + ``violin_bvbrc_synonym_gate``
manifest + agent package + skeletons + composer config + tests — see
``docs/generate_arc_agent_locus_design.md`` companion notes / the both-modes audit). Until that
lands it is the SINGLE documented exception; the test forces a NEW in-DAG-LLM product workflow to
fail rather than silently joining it.
"""

from __future__ import annotations

import logging

import pytest

from apecx_integration.composition.runtime.execution_locus import ExecutionLocus
from apecx_integration.mcp_surface.llm_policy import workflow_needs_llm_at_run
from apecx_integration.mcp_surface.workflow_discovery import discover_workflows
from apecx_integration.mcp_surface.workflow_registry import (
    _load_workflow_for_entry,
    resolve_catalog_entry,
)

# Documented, deliberately-pinned exceptions. An entry here is a workflow KNOWN to require a
# server LLM in desktop — it must be retired or reshaped, not grown. Adding to this set is a
# design decision, not a way to silence the test.
_KNOWN_NOT_DESKTOP_CLEAN = {"violin_bvbrc"}


def _product_workflow_names() -> list[str]:
    return sorted(dw.name for dw in discover_workflows() if dw.category == "product")


def _desktop_clean(name: str) -> bool:
    """True iff running ``name`` in DESKTOP locus needs NO server LLM (both-modes guarantee)."""
    entry = resolve_catalog_entry(name)
    assert entry is not None, f"product workflow {name!r} did not resolve to a runnable entry"
    workflow = _load_workflow_for_entry(entry)
    return not workflow_needs_llm_at_run(workflow, ExecutionLocus.DESKTOP)


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_every_product_workflow_runs_in_desktop_without_a_server_llm():
    """The core invariant. A non-exempt product workflow that needs a server LLM in desktop is a
    both-modes violation — it is agent-shaped and must not be a product workflow."""
    offenders = []
    for name in _product_workflow_names():
        if name in _KNOWN_NOT_DESKTOP_CLEAN:
            continue
        if not _desktop_clean(name):
            offenders.append(name)
    assert not offenders, (
        "these product workflows require a server LLM in DESKTOP locus (not desktop-invertible "
        f"— an in-DAG LLM step the host cannot take over): {offenders}. Make them "
        "final-synthesis-only, fix a heuristic-misflagged deterministic step with "
        "LLM_ROLE='none', or categorize them non-product. See "
        "tests/unit/test_product_workflows_both_modes.py docstring."
    )


def test_known_exceptions_are_still_real_exceptions():
    """Self-cleaning guard: every pinned exception that is STILL a discovered product workflow
    must STILL actually need a desktop LLM. When ``violin_bvbrc`` is retired (removed from the
    product surface) OR reshaped to be desktop-clean, this flips and forces removing it from
    ``_KNOWN_NOT_DESKTOP_CLEAN`` — the set can never carry a stale entry."""
    present = set(_product_workflow_names())
    for name in _KNOWN_NOT_DESKTOP_CLEAN & present:
        assert not _desktop_clean(name), (
            f"{name!r} is now desktop-clean (or retired) — remove it from "
            "_KNOWN_NOT_DESKTOP_CLEAN; the both-modes invariant now holds for it unconditionally."
        )
