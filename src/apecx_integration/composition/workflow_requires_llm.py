"""V4 — compute whether a workflow needs an LLM (the loud-disclosure contract).

Design §9: before running an LLM-bearing workflow the engine must announce which
LLM it resolved, and REFUSE (loudly) when none is available — never silently
degrade. That requires knowing, ahead of execution, whether a workflow (including
any reused sub-workflows) contains an LLM-bearing step.

This is a **best-effort heuristic, and that is acceptable** because the V3 dry-run
backstops a false-negative: an LLM step that needs an LLM and has none will RAISE,
which the dry-run catches. So the cost of a missed LLM step is a caught dry-run
failure, not a silent production degradation. We therefore optimise for PRECISION
on the deterministic side (don't false-positive a deterministic step into a wrong
"needs LLM" refusal) and lean on the dry-run for recall.

Detection: resolve each step's ``class``, inspect its module source for STRONG
LLM-usage markers (an LLM client attr / call / prompt config) — NOT the weak
``APECX_LLM`` env-var string, which appears in deterministic steps' comments
(e.g. IsolatedPyExecStep). Sub-workflow steps are conservatively treated as
LLM-bearing (they usually nest reasoning; over-disclosure here is safe and
dry-run-backstopped).
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any

# Strong = real LLM usage (a client attribute, a call, prompt config). The weak
# ``APECX_LLM`` env string is deliberately EXCLUDED — it appears in the comments
# of deterministic steps (IsolatedPyExecStep) and would false-positive them.
_STRONG_LLM_MARKERS = (
    "_llm",
    "system_prompt",
    "llm_model",
    "prompt_template",
    "build_llm",
    ".invoke(",
    "LLMClient",
    "_apply_llm",
)

# Step classes whose whole purpose is to run an inner workflow — recursed
# (or, when the inner path can't be resolved, conservatively treated as LLM).
_SUBWORKFLOW_CLASS_SUFFIXES = ("SubworkflowStep", "RecursiveSubworkflowStep")


@dataclass
class RequiresLlmResult:
    requires_llm: bool
    llm_steps: list[str] = field(default_factory=list)  # step names detected LLM-bearing
    unresolved_steps: list[str] = field(default_factory=list)  # classes not inspectable
    subworkflow_steps: list[str] = field(default_factory=list)  # conservatively LLM


def _class_is_llm_bearing(class_path: str, _depth: int = 0) -> bool | None:
    """True/False if determinable from the class's module source; None if the
    class can't be resolved/inspected (a novel generated class).

    Two signals: (1) a direct strong LLM-usage marker in the source; (2) the
    step-composition pattern — the source constructs (``from_config``) an
    imported Step class that is itself LLM-bearing (e.g. TdrIterationStep wraps
    CodeWriteStep). Composition recursion is one level deep (``_depth`` guard)
    to stay bounded.
    """
    if not class_path or "." not in class_path:
        return None
    mod_path, _, cls_name = class_path.rpartition(".")
    try:
        module = importlib.import_module(mod_path)
        cls = getattr(module, cls_name, None)
        if cls is None:
            return None
        src = inspect.getsource(inspect.getmodule(cls))
    except Exception:  # noqa: BLE001 — unresolvable/uninspectable → unknown
        return None
    if any(marker in src for marker in _STRONG_LLM_MARKERS):
        return True
    # Step-composition: does this step construct an LLM-bearing inner Step?
    if _depth == 0 and "from_config" in src:
        try:
            from nanobrain.core.step import BaseStep  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return False
        for attr_name in dir(module):
            if not attr_name.endswith("Step") or attr_name == cls_name or attr_name not in src:
                continue
            inner = getattr(module, attr_name, None)
            if isinstance(inner, type) and issubclass(inner, BaseStep):
                inner_path = f"{inner.__module__}.{inner.__name__}"
                if _class_is_llm_bearing(inner_path, _depth=1) is True:
                    return True
    return False


def _is_subworkflow_class(class_path: str) -> bool:
    return any(class_path.endswith(suffix) for suffix in _SUBWORKFLOW_CLASS_SUFFIXES)


def compute_requires_llm(workflow_dict: dict[str, Any]) -> RequiresLlmResult:
    """Compute whether ``workflow_dict`` is LLM-bearing.

    Walks ``steps:``. A step counts as LLM-bearing if its resolved class's source
    carries a strong LLM marker, OR it is a sub-workflow step (conservative). An
    UNRESOLVABLE class (novel generated code) is recorded but does NOT by itself
    flip requires_llm — the dry-run backstops that; over-flagging would false-
    refuse deterministic generated workflows.
    """
    steps = workflow_dict.get("steps") or {}
    res = RequiresLlmResult(requires_llm=False)
    for step_name, step_spec in steps.items():
        class_path = step_spec.get("class") if isinstance(step_spec, dict) else None
        if not isinstance(class_path, str):
            continue
        if _is_subworkflow_class(class_path):
            res.subworkflow_steps.append(step_name)
            res.requires_llm = True
            continue
        verdict = _class_is_llm_bearing(class_path)
        if verdict is True:
            res.llm_steps.append(step_name)
            res.requires_llm = True
        elif verdict is None:
            res.unresolved_steps.append(step_name)
    return res


def loaded_workflow_llm_steps(workflow: Any) -> list[tuple[str, str]]:
    """``[(step_name, llm_role)]`` for each LLM-bearing step of a LOADED workflow.

    ``llm_role`` is the step CLASS's ``LLM_ROLE`` attribute (``"final_synthesis"`` for a
    terminal step that omits its LLM call in desktop locus and lets the host synthesize, else
    ``"in_dag"`` — the default). Used by the run-time gate to decide whether a workflow needs
    an LLM RIGHT NOW: in desktop locus a ``final_synthesis`` step self-omits, so it does not.
    """
    children = (
        getattr(workflow, "child_steps", None)
        or getattr(workflow, "_child_steps", None)
        or getattr(workflow, "steps", None)
        or {}
    )
    out: list[tuple[str, str]] = []
    if not isinstance(children, dict):
        return out
    for name, step in children.items():
        cls = type(step)
        # Explicit declaration wins (incl. ``"none"`` to opt a deterministic step OUT). Unlike
        # ``compute_requires_llm`` — whose conservative "any SubworkflowStep is LLM-bearing"
        # bias is safe for generate-arc DISCLOSURE (dry-run backstops a miss) — a run-time
        # REFUSAL gate needs PRECISION: a false positive wrongly refuses a deterministic
        # workflow (e.g. the MAFFT-only ``sequence`` subworkflow) on a desktop with no LLM. So
        # for un-annotated steps we use ONLY the strong source-marker signal, not the suffix.
        explicit = getattr(cls, "LLM_ROLE", None)
        if explicit is not None:
            if explicit != "none":
                out.append((name, explicit))
            continue
        class_path = f"{cls.__module__}.{cls.__name__}"
        if _class_is_llm_bearing(class_path) is True:
            out.append((name, "in_dag"))
    return out


__all__ = ["RequiresLlmResult", "compute_requires_llm", "loaded_workflow_llm_steps"]
