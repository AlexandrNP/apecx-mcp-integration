"""Dynamic, registration-free discovery of nanobrain workflows on disk.

``list_workflows`` must IDENTIFY every workflow under
``composition/workflows/`` — a NEW workflow directory appears automatically,
with NO entry in any central catalog. The hand-written
``mcp_workflow_catalog.yml`` is now an OPTIONAL OVERRIDE layer (tuned run-hints:
``settle_ms``, envelope keys, ``prewarm_rhea_tools``, prerequisite gates) keyed by
name — it NEVER gates a workflow's existence or discoverability.

A directory is discovered if it carries either definition shape:
  - a YAML workflow (``workflow.yml`` / ``*_workflow.yml`` with top-level ``name:``
    + ``steps:``) → run via ``Workflow.from_config(path)``; or
  - a Python builder (``builder.py`` exposing a public ``build_*`` callable) → run
    via ``module:function``.

Name + one-line description come from the workflow's OWN metadata (the YAML
``name:``/``description:``, or the package ``__init__``/builder docstring) — read
WITHOUT importing the module (AST only; builders have heavy import side effects).

``category`` is a transparent LABEL, never a filter — EVERY workflow is returned:
  - ``benchmark`` — the ``benchmark_`` directory-name prefix (codegen eval scaffolds);
  - ``demo``      — a small set of framework-capability demos / loops;
  - ``product``   — everything else (the default; a new scientist workflow lands here).

Nothing is hidden: a caller that wants only scientist tools filters on ``category``,
it does not rely on us to omit rows.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Directory-name → category for the non-product workflows. Pure labels (every entry is
# still returned). Anything not listed and not ``benchmark_``-prefixed defaults to product.
_DEMO_DIRS = {
    "tdr_loop",
    "best_of_n_loop",
    "code_writing",
    "rhea_muscle_alignment_aurora",
    "open_rosalind_rhea",
}


@dataclass(frozen=True)
class DiscoveredWorkflow:
    """One workflow found on disk, with the metadata read from its own files."""

    name: str
    category: str  # "product" | "benchmark" | "demo"
    description: str
    source: dict  # {"kind": "yaml", "path": ...} | {"kind": "lightweight", "module": ..., "function": ...}


def _workflows_dir() -> Path:
    """Absolute path to ``apecx_integration/composition/workflows``."""
    # __file__ = .../apecx_integration/mcp_surface/workflow_discovery.py
    return Path(__file__).resolve().parents[1] / "composition" / "workflows"


def _first_line(text: str | None) -> str:
    """First non-empty, whitespace-collapsed line of a doc/description block."""
    if not text:
        return ""
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if line:
            return line
    return ""


def _strip_name_prefix(desc: str, name: str) -> str:
    """``"viral_conserved_sites — find ..."`` → ``"find ..."`` (drop a leading ``name —``)."""
    for sep in (" — ", " - ", " – "):
        prefix = f"{name}{sep}"
        if desc.startswith(prefix):
            return desc[len(prefix) :].strip()
    return desc


def category_for(name: str) -> str:
    """Transparent category LABEL for a workflow name (never a filter)."""
    if name.startswith("benchmark_"):
        return "benchmark"
    if name in _DEMO_DIRS or name.endswith("_aurora"):
        return "demo"
    return "product"


# Backwards-compatible private alias (kept for internal callers).
_categorize = category_for


def _module_docstring(py_path: Path) -> str:
    """AST-read a .py file's module docstring WITHOUT importing it."""
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ""
    return ast.get_docstring(tree) or ""


def _public_build_function(builder_py: Path, dir_name: str) -> str | None:
    """Name of the public ``build_*`` callable in ``builder.py`` (AST, no import).

    Prefers ``build_<dir_name>_workflow`` / ``build_<dir_name>``; else the first
    ``build_*`` that is not a private ``_`` helper and not a ``*_muscle_*`` /
    ``*_core_*`` variant (those are catalog-override territory).
    """
    try:
        tree = ast.parse(builder_py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    builds = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("build_")
    ]
    if not builds:
        return None
    for preferred in (f"build_{dir_name}_workflow", f"build_{dir_name}"):
        if preferred in builds:
            return preferred
    # Fall back to the shortest build_* name (the base entry-point, not a variant).
    return min(builds, key=len)


def _discover_yaml_workflow(d: Path) -> DiscoveredWorkflow | None:
    """A directory whose workflow is defined by a YAML file."""
    candidates = sorted(d.glob("*.yml"))
    # Prefer the canonical names, then any yaml that is actually a workflow.
    ordered = (
        [p for p in candidates if p.name == "workflow.yml"]
        + [p for p in candidates if p.name == f"{d.name}_workflow.yml"]
        + [p for p in candidates if p.name.endswith("_workflow.yml")]
        + candidates
    )
    seen: set[Path] = set()
    for p in ordered:
        if p in seen:
            continue
        seen.add(p)
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict) or "steps" not in raw or "name" not in raw:
            continue  # not a workflow YAML (e.g. a step config or manifest)
        rel = p.relative_to(_workflows_dir().parent.parent)  # relative to pkg root
        return DiscoveredWorkflow(
            name=d.name,
            category=_categorize(d.name),
            description=_first_line(str(raw.get("description", ""))),
            source={"kind": "yaml", "path": str(rel)},
        )
    return None


def _discover_builder_workflow(d: Path) -> DiscoveredWorkflow | None:
    """A directory whose workflow is built by a Python ``builder.py``."""
    builder_py = d / "builder.py"
    if not builder_py.is_file():
        return None
    fn = _public_build_function(builder_py, d.name)
    if fn is None:
        return None
    # Description: package __init__ docstring first (cleanest), else the builder module.
    desc = _first_line(_module_docstring(d / "__init__.py")) or _first_line(
        _module_docstring(builder_py)
    )
    return DiscoveredWorkflow(
        name=d.name,
        category=_categorize(d.name),
        description=_strip_name_prefix(desc, d.name),
        source={
            "kind": "lightweight",
            "module": f"apecx_integration.composition.workflows.{d.name}.builder",
            "function": fn,
        },
    )


def discover_workflows() -> list[DiscoveredWorkflow]:
    """Scan ``composition/workflows/`` and return every workflow found, sorted by name.

    One directory → one workflow (the common case). A builder dir resolves to its
    ``build_*`` entry-point; a YAML dir to its canonical workflow YAML. Directories with
    neither a workflow YAML nor a ``builder.py`` are skipped (e.g. a bare ``steps/`` dir).
    """
    root = _workflows_dir()
    if not root.is_dir():
        log.warning("workflow discovery: %s does not exist", root)
        return []

    found: list[DiscoveredWorkflow] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "__pycache__"):
        dw = _discover_builder_workflow(d) or _discover_yaml_workflow(d)
        if dw is not None:
            found.append(dw)
        else:
            log.debug("workflow discovery: %s has no recognizable workflow definition", d.name)
    return found


def discover_by_name(name: str) -> DiscoveredWorkflow | None:
    """The discovered workflow whose name == ``name``, or None."""
    return next((dw for dw in discover_workflows() if dw.name == name), None)


__all__ = ["DiscoveredWorkflow", "category_for", "discover_by_name", "discover_workflows"]
