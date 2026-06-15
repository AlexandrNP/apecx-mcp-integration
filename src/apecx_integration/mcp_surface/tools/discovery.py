"""MCP tools that expose the workflow catalog to the model.

Background — why these exist
----------------------------
Before this module, the integration MCP only exposed
``start_workflow(description, user_id)``. The composer behind that
endpoint retrieves over a fixed set of manifest YAMLs
(``composer_config.yml::component_catalog_paths``) and stitches
together a workflow from the matching components. The problem: the
model has zero visibility into what components / workflows that
catalog actually contains, so it tends to hand the composer a
free-text description that has nothing to do with the available
building blocks. The composer then either fails to retrieve
anything useful or hallucinates a workflow that the executor can't
run.

These tools give the model a discovery surface:

- ``list_workflows()`` returns one row per manifest YAML — workflow
  name, spec-doc pointer, component count, release variant.
- ``describe_workflow(name)`` returns the full per-component view
  (step id, name, status, disposition, rag description, rag
  examples). The model can read this before deciding whether to
  call ``start_workflow`` and what description to pass.

Both tools are read-only. They parse the manifest YAML directly
(rather than going through ``ComponentCatalog.from_manifests``)
because the catalog filters out ``disposition: deferred`` and
empty-description entries — both of which are still informative
for discovery (e.g. "synonym_fuzzy_match exists but is deferred"
keeps the model from re-proposing it).

Resolution order for the manifest list
--------------------------------------
1. ``APECX_COMPOSER_CONFIG`` env var (operator override).
2. The composer config that ships in this package
   (``apecx_integration/composition/composer_config.yml``).

Both paths must exist; missing files raise a clear error rather
than silently returning an empty list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class _ManifestSummary:
    """Internal: one parsed manifest reduced to a dict-friendly shape."""

    workflow_name: str
    manifest_path: Path
    spec_doc: str | None
    first_release_variant: str | None
    components: list[dict[str, Any]]


def _resolve_composer_config_path() -> Path:
    """Find the composer config the manifest paths are relative to."""
    override = os.environ.get("APECX_COMPOSER_CONFIG")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"APECX_COMPOSER_CONFIG={override!r} does not point at "
                "a file. Either fix the env var or unset it to fall "
                "back to the packaged default."
            )
        return path

    import apecx_integration.composition as _composition

    pkg_dir = Path(_composition.__file__).resolve().parent
    path = pkg_dir / "composer_config.yml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Packaged composer config not found at {path}. The "
            "discovery tools cannot enumerate workflows without it."
        )
    return path


def _load_manifest_paths(config_path: Path) -> list[Path]:
    """Return the list of manifest YAMLs the composer is configured to
    use, resolved relative to ``config_path``'s parent directory."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"composer config at {config_path} is not a YAML mapping")
    paths_field = raw.get("component_catalog_paths") or []
    if not isinstance(paths_field, list):
        raise ValueError(
            f"component_catalog_paths in {config_path} must be a list, "
            f"got {type(paths_field).__name__}"
        )
    base = config_path.parent
    out: list[Path] = []
    for entry in paths_field:
        if not isinstance(entry, str):
            continue
        out.append((base / entry).resolve())
    return out


def _parse_manifest(path: Path) -> _ManifestSummary:
    """Parse one manifest YAML into the internal summary shape."""
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"manifest at {path} must be a YAML mapping at the top level")

    workflow_block = raw.get("workflow") or {}
    if not isinstance(workflow_block, dict):
        workflow_block = {}
    workflow_name = workflow_block.get("name") or path.parent.name
    spec_doc = workflow_block.get("spec")
    first_release_variant = workflow_block.get("first_release_variant")

    components_field = raw.get("components") or []
    if not isinstance(components_field, list):
        components_field = []

    components: list[dict[str, Any]] = []
    for entry in components_field:
        if not isinstance(entry, dict):
            continue
        # Keep deferred components in the output: discovery should
        # show the full picture so the model knows what's NOT yet
        # available rather than re-proposing it.
        components.append(
            {
                "step_id": str(entry.get("step_id", "")),
                "step_name": str(entry.get("step_name", "")),
                "disposition": entry.get("disposition"),
                "status": entry.get("status"),
                "class_path": entry.get("class"),
                "yaml_path": entry.get("yaml"),
                "rag_description": _strip(entry.get("rag_description", "")),
                "rag_examples": [
                    _strip(e) for e in (entry.get("rag_examples") or []) if isinstance(e, str)
                ],
            }
        )

    return _ManifestSummary(
        workflow_name=str(workflow_name),
        manifest_path=path,
        spec_doc=str(spec_doc) if spec_doc else None,
        first_release_variant=(str(first_release_variant) if first_release_variant else None),
        components=components,
    )


def _strip(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    # Collapse YAML folded-scalar whitespace runs into single spaces.
    return " ".join(s.split()).strip()


def _load_all_manifests() -> list[_ManifestSummary]:
    config_path = _resolve_composer_config_path()
    manifest_paths = _load_manifest_paths(config_path)
    out: list[_ManifestSummary] = []
    for p in manifest_paths:
        out.append(_parse_manifest(p))
    return out


# ---------------------------------------------------------------------------
# Public MCP tools
# ---------------------------------------------------------------------------


def _load_runnable_catalog() -> tuple[list[dict[str, Any]], str | None]:
    """Rows for the directly-runnable workflows (``run_workflow`` targets), DYNAMICALLY.

    Built by scanning ``composition/workflows/`` (``workflow_discovery``) — EVERY workflow
    on disk appears, with NO central registration. The hand-written
    ``mcp_workflow_catalog.yml`` is merged in only as an OVERRIDE (tuned prerequisites /
    run-hints) for the workflows that declare it; it never gates which workflows are
    listed. A new workflow directory shows up here automatically.

    Each row carries ``name``, ``description``, ``category`` (transparent label:
    ``product`` | ``benchmark`` | ``demo`` — nothing is hidden, only labeled),
    ``invoke_with="run_workflow"``, an ``available`` flag (+ ``missing_prerequisites`` /
    ``unavailable_hint`` when a backend is unconfigured), and ``tuned`` (does a catalog
    override exist?). Rows are sorted product-first, then by name.

    Returns ``(rows, error)``. On a discovery/catalog failure returns ``([], "<message>")``
    — surfaced LOUDLY in the response, never a silent empty list.
    """
    try:
        from apecx_integration.mcp_surface.workflow_discovery import (
            category_for,
            discover_workflows,
        )
        from apecx_integration.mcp_surface.workflow_registry import (
            check_prerequisites,
            load_catalog,
        )

        discovered = discover_workflows()
        catalog = load_catalog()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash discovery
        return [], f"{type(exc).__name__}: {exc}"

    cat_by_name = {e.tool_name: e for e in catalog.workflows}

    def _row(name: str, category: str, default_description: str) -> dict[str, Any]:
        entry = cat_by_name.get(name)
        if entry is not None:
            met, missing = check_prerequisites(entry.requires)
            return {
                "name": name,
                "kind": "runnable",
                "invoke_with": "run_workflow",
                "category": category,
                "description": _strip(entry.description) or default_description,
                "available": met,
                "missing_prerequisites": missing,
                "unavailable_hint": entry.requires.unavailable_hint if not met else "",
                "input_schema": entry.input_schema,
                "tuned": True,
            }
        # Discovered but not cataloged: runnable now (no declared prereqs); run-hints are
        # auto-derived at run time. ``available`` is True because nothing is known to block
        # it — a real missing backend surfaces as a loud failure at run, never a silent skip.
        return {
            "name": name,
            "kind": "runnable",
            "invoke_with": "run_workflow",
            "category": category,
            "description": default_description,
            "available": True,
            "missing_prerequisites": [],
            "unavailable_hint": "",
            "input_schema": {},
            "tuned": False,
        }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dw in discovered:
        seen.add(dw.name)
        rows.append(_row(dw.name, dw.category, _strip(dw.description)))
    # Catalog entries with no matching directory (e.g. a second variant built from the same
    # builder, like ``viral_conserved_sites_muscle``) are still real runnable tools.
    for entry in catalog.workflows:
        if entry.tool_name in seen:
            continue
        rows.append(_row(entry.tool_name, category_for(entry.tool_name), _strip(entry.description)))

    # Product first (the scientist-facing set), then benchmark/demo — sorted, never hidden.
    _order = {"product": 0, "demo": 1, "benchmark": 2}
    rows.sort(key=lambda r: (_order.get(r["category"], 9), r["name"]))
    return rows, None


async def list_workflows() -> dict:
    """Find a specialized apecx workflow for a science question BEFORE answering from memory.

    Call this FIRST whenever the user asks about viruses, vaccines, pathogens, genes or
    proteins, epitopes / antigens, protein structures, or sequence conservation — it lists
    the workflows that return GROUNDED, CITED evidence from real curated data (BV-BRC,
    VIOLIN, PDB/EMDB, PubMed, the APECx Globus corpus) instead of model knowledge. Then run
    the best match with ``run_workflow(name, params)``. (``apecx_capabilities`` is the
    one-call superset: these workflows PLUS the standalone primitive tools and live backend
    health.)

    Two distinct kinds, by how you invoke them (external_orchestration_design.md §4):

    - ``runnable`` (under the ``runnable`` key): EVERY workflow discovered on disk
      (``composition/workflows/``), driven directly with ``run_workflow(name, params)`` —
      NO central registration; a new workflow directory appears here automatically. Each
      row carries ``name``, ``description``, ``category`` (``product`` | ``benchmark`` |
      ``demo`` — a transparent label, nothing is hidden), ``available`` (+
      ``missing_prerequisites`` when a backend/env is not configured), and ``tuned`` (does
      a catalog override of its run-hints exist?). THIS is the path for "do task X now".
    - ``composable`` (under the ``workflows`` key): manifests the composer can stitch a
      NEW workflow from, via ``start_workflow(description)``. Use when no runnable
      workflow matches and a new one must be composed.

    Composable rows carry: ``workflow_name``, ``manifest_path``, ``spec_doc``,
    ``first_release_variant``, ``num_components`` / ``num_ready`` / ``num_deferred``,
    ``component_names``. A ``runnable_error`` key appears only if the runnable catalog
    failed to load (loud, not silent).
    """
    summaries = _load_all_manifests()
    rows: list[dict[str, Any]] = []
    for s in summaries:
        ready = sum(1 for c in s.components if (c.get("status") or "").startswith("ready"))
        deferred = sum(1 for c in s.components if c.get("disposition") == "deferred")
        rows.append(
            {
                "workflow_name": s.workflow_name,
                "kind": "composable",
                "invoke_with": "start_workflow",
                "manifest_path": str(s.manifest_path),
                "spec_doc": s.spec_doc,
                "first_release_variant": s.first_release_variant,
                "num_components": len(s.components),
                "num_ready": ready,
                "num_deferred": deferred,
                "component_names": [c["step_name"] for c in s.components],
            }
        )
    runnable, runnable_error = _load_runnable_catalog()
    out: dict[str, Any] = {
        "workflows": rows,
        "runnable": runnable,
        "count": len(rows),
        "runnable_count": len(runnable),
    }
    if runnable_error is not None:
        out["runnable_error"] = runnable_error
    return out


async def describe_workflow(name: str) -> dict:
    """Return the full per-component view of one workflow.

    ``name`` matches against ``workflow.name`` in each manifest.
    The match is case-sensitive (workflow names are stable ids).
    On miss, returns an error payload that lists the available
    workflow names so the caller can self-correct without a
    second tool call.

    Each component carries: step_id, step_name, disposition,
    status, class_path, yaml_path, rag_description, rag_examples.
    The model should read ``rag_description`` + ``rag_examples``
    to pick a phrasing for ``start_workflow.description`` that
    will retrieve the right components.
    """
    if not isinstance(name, str) or not name.strip():
        return {
            "error": "describe_workflow requires a non-empty workflow name",
            "available": [s.workflow_name for s in _load_all_manifests()],
        }
    summaries = _load_all_manifests()
    for s in summaries:
        if s.workflow_name == name:
            return {
                "workflow_name": s.workflow_name,
                "manifest_path": str(s.manifest_path),
                "spec_doc": s.spec_doc,
                "first_release_variant": s.first_release_variant,
                "components": s.components,
            }
    return {
        "error": f"unknown workflow {name!r}",
        "available": [s.workflow_name for s in summaries],
    }


__all__ = ["describe_workflow", "list_workflows"]
