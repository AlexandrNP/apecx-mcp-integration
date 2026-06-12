"""Derive a workflow's required inputs from its OWN schema (RoC-2b).

Single source of truth = the nanobrain workflow, not the catalog. The entry step (the one whose
input data unit is the catalog `input_envelope_key`) declares a G6 `step_input_schema`; this module
resolves it, unwraps the trigger-envelope level, and reports the required params (+ types +
`obtain_via` hints). `run_workflow` (RoC-2c) uses this to return `needs_input` on missing/ill-typed
params instead of failing deep in the cascade — control returns to the frontier LLM.

Degrade-loud, never crash: a workflow whose entry step declares NO schema has no derivable required
params (returns empty + a log note) — an unconstrained workflow legitimately has none.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def derive_required_inputs(workflow: Any, entry_du_name: str | None) -> dict[str, Any]:
    """Return ``{required, properties, obtain_via}`` from the entry step's ``step_input_schema``."""
    entry_step = _find_entry_step(workflow, entry_du_name)
    if entry_step is None:
        log.info(
            "derive_required_inputs: no entry step for du %r; no required params", entry_du_name
        )
        return {"required": [], "properties": {}, "obtain_via": {}}
    schema_ref = _resolve_g6_input_schema(entry_step)
    if schema_ref is None or getattr(schema_ref, "json_schema", None) is None:
        return {"required": [], "properties": {}, "obtain_via": {}}
    param_schema = _unwrap_param_schema(schema_ref.json_schema, entry_du_name)
    props = param_schema.get("properties", {}) or {}
    return {
        "required": list(param_schema.get("required", []) or []),
        "properties": props,
        "obtain_via": {
            name: prop["obtain_via"]
            for name, prop in props.items()
            if isinstance(prop, dict) and prop.get("obtain_via")
        },
    }


def find_param_gaps(params: dict[str, Any], derived: dict[str, Any]) -> list[Any]:
    """Return a list of ``ParamNeed`` for required params that are missing or ill-typed."""
    from apecx_integration.composition.schemas.control_transfer import ParamNeed

    props = derived.get("properties", {})
    obtain_via = derived.get("obtain_via", {})
    needs: list[Any] = []
    for name in derived.get("required", []):
        prop = props.get(name, {})
        present = name in params and not _is_blank(params[name])
        if not present:
            needs.append(
                ParamNeed(
                    param_name=name,
                    issue="missing",
                    param_schema=prop or None,
                    obtain_via=obtain_via.get(name),
                )
            )
        elif not _type_ok(params[name], prop):
            needs.append(
                ParamNeed(
                    param_name=name,
                    issue="ill_typed",
                    param_schema=prop or None,
                    obtain_via=obtain_via.get(name),
                )
            )
    return needs


# ----- internals -----
def _find_entry_step(workflow: Any, entry_du_name: str | None) -> Any:
    children = (
        getattr(workflow, "child_steps", None)
        or getattr(workflow, "_child_steps", None)
        or getattr(workflow, "steps", None)
        or {}
    )
    if not entry_du_name or not isinstance(children, dict):
        return None
    for step in children.values():
        dus = getattr(step, "step_input_data_units", {}) or {}
        if entry_du_name in dus:
            return step
    return None


def _resolve_g6_input_schema(step: Any) -> Any:
    fn = getattr(step, "_g6_resolved_input_schema", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — schema resolution is best-effort for derivation
        log.warning(
            "derive_required_inputs: G6 schema resolve failed on %s (%s)",
            getattr(step, "name", "?"),
            exc,
        )
        return None


def _unwrap_param_schema(schema: dict[str, Any], entry_du_name: str | None) -> dict[str, Any]:
    """The G6 schema validates the trigger-WRAPPED input ({<entry_du>: {params}}). Unwrap that one
    level to the parameter-dict schema the caller actually supplies."""
    props = schema.get("properties", {}) or {}
    if (
        entry_du_name
        and list(props.keys()) == [entry_du_name]
        and isinstance(props.get(entry_du_name), dict)
        and "properties" in props[entry_du_name]
    ):
        return props[entry_du_name]
    return schema


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _type_ok(value: Any, prop_schema: dict[str, Any]) -> bool:
    t = prop_schema.get("type")
    if t == "integer":
        # Match the step's own tolerance: a digit string is acceptable for a taxon id.
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, str) and value.strip().isdigit()
        )
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    return True  # unknown/unconstrained type → don't reject


__all__ = ["derive_required_inputs", "find_param_gaps"]
