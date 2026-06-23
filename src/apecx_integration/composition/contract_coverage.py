"""Contract coverage analysis over the workflow corpus (Project A Step 3a).

Counts DirectLink BOUNDARIES that are NOT both-endpoints-contract-declared ("undeclared
boundaries"). This powers the WARN-RATCHET: a pinned test asserts the count never INCREASES,
so the corpus monotonically gains contract coverage as contributors annotate their workflows
(the contract system's gradual, multi-contributor premise).

A boundary is COVERED iff BOTH endpoints declare a ``contract`` — matching the load-time
WARN-checker's "skip unless both declared" (nanobrain Workflow._check_link_contracts). So the
count here is exactly the number of boundaries the checker currently cannot enforce.

Endpoint resolution mirrors the T7 migration audit: a ``step.du`` ref resolves to the step's
wrapper YAML (via the workflow dir + catalog roots) and reads the relevant data unit's
contract; a bare ref (``workflow_input`` / ``workflow_output`` / an intermediate) reads the
workflow YAML's own data units. Pure analysis, no execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from apecx_integration.composition.component_catalog import catalog_search_roots

_CFGDIR = Path(__file__).resolve().parent
_WF_DIR = _CFGDIR / "workflows"


def _resolve(ref: str, base: Path, roots: list[str]) -> Path | None:
    """Resolve a relative ``config:`` ref to a file, base_path first then catalog roots."""
    from datetime import datetime

    from nanobrain.core.config.config_base import ConfigLoadingContext
    from nanobrain.core.workflow import WorkflowConfig

    ctx = ConfigLoadingContext(
        base_path=base,
        resolution_stack=set(),
        loading_timestamp=datetime.now(),
        config_search_paths=roots,
    )
    try:
        resolved = WorkflowConfig._resolve_config_path(ref, ctx)
        return Path(resolved) if resolved else None
    except Exception:
        return None


def _load(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _endpoint_contract(ref: str, side: str, wf: dict, base: Path, roots: list[str]) -> Any:
    """The declared contract for a link endpoint, or None when undeclared/unresolvable.

    ``side`` is 'source' (producer output) or 'target' (consumer input) — picks
    output_data_units vs input_data_units for a ``step.du`` ref.

    PROXY CAVEAT (Step 3a): the runtime resolver (Workflow._resolve_data_unit_reference) looks
    output-first-then-input by DU NAME, not strictly by side. This side-based mapping agrees
    except when a step declares the SAME du name in BOTH its input and output blocks AND it's a
    link endpoint — absent in the current corpus. Resolution failures return None → counted as
    undeclared (conservative: the ratchet only ever over-counts, never loosens). 3b's full-corpus
    pass should reconcile this if any same-name-both-blocks DU appears.
    """
    du_block = "output_data_units" if side == "source" else "input_data_units"
    if "." in ref:
        step_id, du_name = ref.split(".", 1)
        step = (wf.get("steps") or {}).get(step_id) or {}
        cfg = step.get("config")
        if not isinstance(cfg, str):
            return None  # inline step config is not used in the corpus; treat as undeclared
        wrapper_path = _resolve(cfg, base, roots)
        if wrapper_path is None or not wrapper_path.is_file():
            return None
        wrapper = _load(wrapper_path)
        du = (wrapper.get(du_block) or {}).get(du_name) or {}
        return du.get("contract")
    # bare ref -> a workflow-level data unit (check both blocks)
    for block in ("input_data_units", "output_data_units"):
        du = (wf.get(block) or {}).get(ref)
        if du:
            return du.get("contract")
    return None


def _direct_links(wf: dict) -> list[tuple[str, str]]:
    """(source, target) for each DirectLink with both refs present.

    Scope (Step 3a): DirectLink only — the corpus is overwhelmingly DirectLink. The runtime
    checker runs on every link class, so a ConditionalLink boundary losing coverage would not
    trip this ratchet; 3b should widen this if ConditionalLink boundaries proliferate.
    """
    out: list[tuple[str, str]] = []
    for _name, link in (wf.get("links") or {}).items():
        if not isinstance(link, dict):
            continue
        if "DirectLink" not in str(link.get("class", "")):
            continue
        cfg = link.get("config")
        if not isinstance(cfg, dict):
            continue
        src, tgt = cfg.get("source"), cfg.get("target")
        if isinstance(src, str) and isinstance(tgt, str):
            out.append((src, tgt))
    return out


def workflow_yamls(wf_dir: Path = _WF_DIR) -> list[Path]:
    """Top-level workflow YAMLs (those declaring both steps and links) under ``wf_dir``."""
    found: list[Path] = []
    for p in sorted(wf_dir.rglob("*.yml")):
        d = _load(p)
        if isinstance(d, dict) and d.get("steps") and d.get("links"):
            found.append(p)
    return found


def undeclared_boundaries(
    roots: list[str] | None = None, wf_dir: Path = _WF_DIR
) -> list[tuple[str, str, str]]:
    """(workflow_rel_path, source, target) for every DirectLink boundary that is NOT
    both-endpoints-contract-declared. Length = the ratchet count. ``wf_dir`` overrides the
    corpus root (for testing on a synthetic fixture)."""
    if roots is None:
        roots = catalog_search_roots()
    undeclared: list[tuple[str, str, str]] = []
    for wf_path in workflow_yamls(wf_dir):
        wf = _load(wf_path)
        base = wf_path.parent
        for src, tgt in _direct_links(wf):
            src_c = _endpoint_contract(src, "source", wf, base, roots)
            tgt_c = _endpoint_contract(tgt, "target", wf, base, roots)
            if not (src_c and tgt_c):
                undeclared.append((str(wf_path.relative_to(wf_dir)), src, tgt))
    return undeclared


def count_undeclared(roots: list[str] | None = None, wf_dir: Path = _WF_DIR) -> int:
    return len(undeclared_boundaries(roots, wf_dir))


__all__ = ["count_undeclared", "undeclared_boundaries", "workflow_yamls"]
