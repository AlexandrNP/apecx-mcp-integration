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


_DEFAULT_COMPOSER_CONFIG_REL = (
    "apecx_integration/composition/composer_config.yml"
)


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
        raise ValueError(
            f"composer config at {config_path} is not a YAML mapping"
        )
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
        raise ValueError(
            f"manifest at {path} must be a YAML mapping at the top level"
        )

    workflow_block = raw.get("workflow") or {}
    if not isinstance(workflow_block, dict):
        workflow_block = {}
    workflow_name = (
        workflow_block.get("name")
        or path.parent.name
    )
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
        components.append({
            "step_id": str(entry.get("step_id", "")),
            "step_name": str(entry.get("step_name", "")),
            "disposition": entry.get("disposition"),
            "status": entry.get("status"),
            "class_path": entry.get("class"),
            "yaml_path": entry.get("yaml"),
            "rag_description": _strip(entry.get("rag_description", "")),
            "rag_examples": [
                _strip(e) for e in (entry.get("rag_examples") or [])
                if isinstance(e, str)
            ],
        })

    return _ManifestSummary(
        workflow_name=str(workflow_name),
        manifest_path=path,
        spec_doc=str(spec_doc) if spec_doc else None,
        first_release_variant=(
            str(first_release_variant) if first_release_variant else None
        ),
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


async def list_workflows() -> dict:
    """List the workflows the composer can build from.

    Returns a structured catalog of every workflow whose manifest
    is registered in the composer config. Use this BEFORE calling
    ``start_workflow`` to discover what the integration MCP can
    actually compose; if your intent doesn't match any listed
    workflow, ``start_workflow`` will likely produce a workflow
    that doesn't run.

    Each row carries:
      - ``workflow_name``: stable id (e.g. "violin_bvbrc_synonym_gate")
      - ``manifest_path``: absolute path to the manifest YAML
      - ``spec_doc``: relative path to the workflow spec markdown
      - ``first_release_variant``: which variant ships in v1
      - ``num_components`` / ``num_ready`` / ``num_deferred``
      - ``component_names``: ordered list of step_name strings
    """
    summaries = _load_all_manifests()
    rows: list[dict[str, Any]] = []
    for s in summaries:
        ready = sum(
            1 for c in s.components if (c.get("status") or "").startswith("ready")
        )
        deferred = sum(
            1 for c in s.components if c.get("disposition") == "deferred"
        )
        rows.append({
            "workflow_name": s.workflow_name,
            "manifest_path": str(s.manifest_path),
            "spec_doc": s.spec_doc,
            "first_release_variant": s.first_release_variant,
            "num_components": len(s.components),
            "num_ready": ready,
            "num_deferred": deferred,
            "component_names": [c["step_name"] for c in s.components],
        })
    return {"workflows": rows, "count": len(rows)}


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
