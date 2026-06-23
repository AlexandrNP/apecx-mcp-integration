"""T7: resolution migration audit (RELEASE GATE).

Proves the config_search_paths resolver (nanobrain Strategy 7 + the executor injecting
catalog roots) does NOT silently change which file ANY existing workflow's `config:` ref
resolves to. For every step/link config ref across every workflow YAML under
composition/workflows/, resolve it TWO ways with the workflow's own dir as base_path:
  OLD  = empty config_search_paths (Strategy 7 is a no-op -> pre-change behavior)
  NEW  = the PRODUCTION catalog roots (what the executor injects)
and assert every ref that resolved under OLD resolves to the BYTE-IDENTICAL path under NEW.

Existing workflows reference co-located wrappers (steps/X.yml in their own dir), which
resolve via Strategy 2 (base_path) BEFORE Strategy 7 -> expected diff is ZERO. A nonzero
diff is a real silent-resolution regression and fails the gate. Real files, no mocks.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from nanobrain.core.config.config_base import ConfigLoadingContext
from nanobrain.core.workflow import WorkflowConfig

import apecx_integration
from apecx_integration.composition.component_catalog import catalog_search_roots

_CFGDIR = Path(apecx_integration.__file__).parent / "composition"
_WF_DIR = _CFGDIR / "workflows"
_PROD_ROOTS = catalog_search_roots(_CFGDIR / "composer_config.yml")


def _ctx(base: Path, roots: list[str] | None) -> ConfigLoadingContext:
    return ConfigLoadingContext(
        base_path=base,
        resolution_stack=set(),
        loading_timestamp=datetime.now(),
        config_search_paths=roots,
    )


def _resolve(ref: str, base: Path, roots: list[str] | None) -> str | None:
    try:
        return WorkflowConfig._resolve_config_path(ref, _ctx(base, roots))
    except Exception:
        return None


def _config_refs(node) -> list[str]:
    """Collect every string `config:` value reachable in a workflow dict (steps + links)."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "config" and isinstance(v, str):
                out.append(v)
            else:
                out.extend(_config_refs(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_config_refs(item))
    return out


def _workflow_yamls() -> list[Path]:
    out = []
    for p in _WF_DIR.rglob("*.yml"):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("steps"), dict):
            out.append(p)
    return out


def test_production_roots_nonempty():
    # Guard: the audit is meaningless if roots are empty (N1).
    assert len(_PROD_ROOTS) == 4


def test_no_existing_resolution_changes_under_new_resolver():
    workflows = _workflow_yamls()
    assert workflows, "no workflow YAMLs found — audit would be vacuous"
    changed: list[tuple[str, str, str, str]] = []  # (wf, ref, old, new)
    newly_resolved: list[tuple[str, str]] = []
    checked = 0
    for wf in workflows:
        base = wf.parent
        for ref in _config_refs(yaml.safe_load(wf.read_text(encoding="utf-8"))):
            checked += 1
            old = _resolve(ref, base, [])
            new = _resolve(ref, base, _PROD_ROOTS)
            if old is not None and new != old:
                changed.append((str(wf.relative_to(_WF_DIR)), ref, old, str(new)))
            elif old is None and new is not None:
                newly_resolved.append((str(wf.relative_to(_WF_DIR)), ref))
    assert checked > 0, "no config refs extracted — audit would be vacuous"
    # The release gate: NO successful resolution may change.
    assert not changed, f"resolution silently CHANGED for {len(changed)} ref(s): {changed[:10]}"
    # Co-located existing workflows resolve via base_path -> expect zero newly-resolved.
    # (Report, don't hard-fail, so a future legit catalog-reuse workflow is visible.)
    assert not newly_resolved, (
        f"{len(newly_resolved)} ref(s) newly resolve via catalog roots — expected 0 for the "
        f"existing co-located workflows; investigate: {newly_resolved[:10]}"
    )
