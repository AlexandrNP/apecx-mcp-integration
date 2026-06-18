"""Dynamic workflow discovery — list_workflows must IDENTIFY every workflow on disk.

The defect this guards: ``list_workflows`` used to read a 4-entry hand catalog
(``mcp_workflow_catalog.yml``), so real product workflows (``harmonized_search``,
``rag_e2e_synthesis``, ``violin_bvbrc``) were INVISIBLE despite existing on disk. Discovery
is now a filesystem scan; a new workflow directory appears automatically with NO catalog
registration. The catalog is an OPTIONAL run-hint override, not a visibility gate.
"""

from __future__ import annotations

import asyncio

from apecx_integration.mcp_surface.tools.discovery import _load_runnable_catalog, list_workflows
from apecx_integration.mcp_surface.workflow_discovery import (
    category_for,
    discover_by_name,
    discover_workflows,
)
from apecx_integration.mcp_surface.workflow_registry import load_catalog, resolve_catalog_entry

# The scientist-facing workflows that must always be discovered from their own dirs.
_PRODUCT = {
    "conserved_epitope_candidate_assessment",
    "harmonized_search",
    "rag_e2e_synthesis",
    "rhea_muscle_alignment",
    "viral_conserved_sites",
    "viral_epitope_analysis",
}


def test_discover_finds_all_product_workflows():
    names = {dw.name for dw in discover_workflows()}
    missing = _PRODUCT - names
    assert not missing, f"discovery missed product workflows on disk: {missing}"


def test_discovery_carries_self_described_metadata():
    by_name = {dw.name: dw for dw in discover_workflows()}
    # YAML-defined: description from the workflow's own `description:`.
    hs = by_name["harmonized_search"]
    assert hs.source["kind"] == "yaml"
    assert hs.description and "synonym dictionary" in hs.description.lower()
    # Builder-defined: description from the package __init__ docstring, name-prefix stripped.
    ep = by_name["viral_epitope_analysis"]
    assert ep.source["kind"] == "lightweight"
    assert ep.description and not ep.description.startswith("viral_epitope_analysis")


def test_category_is_a_label_not_a_filter():
    assert category_for("benchmark_direct_codegen") == "benchmark"
    assert category_for("tdr_loop") == "demo"
    assert category_for("viral_epitope_analysis") == "product"
    # Every benchmark/demo is STILL discovered (labeled, not hidden).
    names = {dw.name for dw in discover_workflows()}
    assert "benchmark_direct_codegen" in names
    assert "tdr_loop" in names


def test_list_workflows_returns_far_more_than_the_old_catalog():
    out = asyncio.run(list_workflows())
    runnable = out["runnable"]
    names = {r["name"] for r in runnable}
    # The 4-entry catalog used to hide these — they must now be present.
    assert names >= _PRODUCT, f"still hidden: {_PRODUCT - names}"
    assert len(runnable) > 4
    # Catalog is override-only: a discovered, non-cataloged workflow is present + tuned=False.
    cataloged = {e.tool_name for e in load_catalog().workflows}
    non_cataloged = [r for r in runnable if r["name"] not in cataloged]
    assert non_cataloged, "expected discovered-but-not-cataloged workflows in the list"
    assert all(r["tuned"] is False for r in non_cataloged)
    # Every row carries the transparent category label.
    assert all(r["category"] in {"product", "benchmark", "demo"} for r in runnable)


def test_runnable_rows_are_product_first():
    rows, err = _load_runnable_catalog()
    assert err is None, err
    categories = [r["category"] for r in rows]
    # All 'product' rows precede the first non-product row.
    first_non_product = next((i for i, c in enumerate(categories) if c != "product"), len(rows))
    assert all(c == "product" for c in categories[:first_non_product])


def test_resolve_synthesizes_entry_for_non_cataloged_workflow():
    # harmonized_search is NOT in the catalog, but it must resolve to a runnable entry
    # (so run_workflow('harmonized_search') is not "unknown workflow").
    entry = resolve_catalog_entry("harmonized_search")
    assert entry is not None
    assert entry.tool_name == "harmonized_search"
    assert entry.source.kind == "yaml"
    # A genuinely unknown name still resolves to None (loud "unknown" at the call site).
    assert resolve_catalog_entry("definitely_not_a_workflow_xyz") is None
    assert discover_by_name("definitely_not_a_workflow_xyz") is None
