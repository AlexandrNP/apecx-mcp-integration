"""T07 pre-submission allocation estimator (minimal implementation).

**Scope deliberately small** — per user directive 2026-04-23 "don't
over-engineer it." This ships the CORE heuristic as a pure function;
API wiring (``/hpc/estimate`` is still 501) and DB persistence are
deferred until there's a concrete caller.

What this module does
---------------------
One public function: ``estimate_workflow_cost(workflow_config, endpoint)``
→ returns an ``EstimateCostResponse`` carrying ``total_core_hours``,
``per_step_core_hours``, and a deliberately-wide confidence interval.

Heuristic
---------
For each step in the workflow's ``steps:`` block:

1. If the step's config contains an ``estimated_core_hours`` field,
   use that value.
2. Otherwise, apply a per-step-class default:
   - Steps whose ``class:`` contains ``LLM`` / ``Agent`` / ``Ollama``
     → 0.05 core-hours (roughly 3 minutes of LLM wall time).
   - Steps whose ``class:`` contains ``Snapshot`` / ``FileReader``
     → 0.01 core-hours (fast disk I/O).
   - Anything else → 0.1 core-hours (generic fallback).
3. Sum across all steps. Multiply by an endpoint factor.

Endpoint factor (dumb but explicit):
  - ``local``        → 1.0   (baseline)
  - any other name   → 1.0   (placeholder; per-endpoint pricing is
                              future work when real HPC endpoints
                              are reachable)

Confidence interval
-------------------
Deliberately wide: ``(0.3 × total, 3.0 × total)`` — reflecting that
the underlying heuristic is guessing. A narrower interval would
over-promise. AP §5.7 brutal-truth: "T07 is explicitly not about
accuracy, only about the gate." The gate question is "is this run
~10 core-hours or ~1000 core-hours?" The wide interval still
answers that.

What this module deliberately does NOT do
-----------------------------------------
- **Fetch the Run from the DB**. Caller passes a workflow config
  dict; they deal with the lookup.
- **Load LLM-model-specific cost tables**. Future work; for now one
  flat per-step default per class keyword.
- **Historical calibration** (learn from actuals vs. estimates).
  No ``actual_core_hours`` back-propagation yet.
- **Per-endpoint pricing**. All endpoints treated equally at factor 1.0.
- **Novel-Python cap**. The ``novel_python_capped_at`` field on
  ``EstimateCostResponse`` is hard-coded to ``None`` here — meaning
  no cap. T13 sandbox already rejects unknown imports, which is the
  primary safety control.

AP §5.7 brutal-truth disclosure
-------------------------------
This is the implementation of what AP §5.7 calls "item #7 — Pre-
submission allocation accounting", with the Round-3 demotion
("fire-on-export-only") acknowledged. The estimator runs against
ANY workflow config today; it doesn't gate anything. Gating happens
at submit_hpc time (T04/T05 scope) by comparing the estimate to a
user budget, which is separate work.
"""

from __future__ import annotations

from typing import Any

from apecx_integration.control_plane.schemas.api import EstimateCostResponse

# Per-step-class heuristic defaults. Keys are substrings that match
# against the step's ``class`` field (case-insensitive).
_DEFAULT_CORE_HOURS_BY_CLASS_SUBSTRING: tuple[tuple[str, float], ...] = (
    ("LLM", 0.05),
    ("Agent", 0.05),
    ("Ollama", 0.05),
    ("Snapshot", 0.01),
    ("FileReader", 0.01),
    ("DelimitedFileReader", 0.01),
)

# Generic fallback when no class-substring matches.
_GENERIC_FALLBACK_CORE_HOURS: float = 0.1

# Confidence-interval band. (low, high) multipliers applied to the
# total_core_hours. Wide by design.
_CONFIDENCE_LOW_MULTIPLIER: float = 0.3
_CONFIDENCE_HIGH_MULTIPLIER: float = 3.0

# Hours-to-seconds conversion for wall-time, assuming sequential
# execution (upper bound — parallelism only reduces wall time).
_SECONDS_PER_HOUR: float = 3600.0


def _per_step_core_hours(step_id: str, step_config: dict[str, Any]) -> float:
    """Pick a core-hour estimate for a single step.

    Order of precedence:
      1. Explicit ``estimated_core_hours`` field in the step's config
         (if the step author provided one, trust them).
      2. Class-substring match against
         ``_DEFAULT_CORE_HOURS_BY_CLASS_SUBSTRING``.
      3. Generic fallback.
    """
    explicit = step_config.get("estimated_core_hours")
    if explicit is not None:
        return float(explicit)

    class_path = str(step_config.get("class", ""))
    class_lower = class_path.lower()
    for substring, value in _DEFAULT_CORE_HOURS_BY_CLASS_SUBSTRING:
        if substring.lower() in class_lower:
            return value

    return _GENERIC_FALLBACK_CORE_HOURS


def _endpoint_factor(endpoint: str) -> float:
    """Per-endpoint multiplier on the total.

    Deliberately flat for now — per-endpoint pricing is future work.
    The function exists so callers can pass endpoint=... and the
    signature doesn't change when pricing lands.
    """
    _ = endpoint  # currently unused; preserved for future extension
    return 1.0


def estimate_workflow_cost(
    workflow_config: dict[str, Any],
    endpoint: str = "local",
) -> EstimateCostResponse:
    """Estimate the compute cost of running a workflow.

    Args:
        workflow_config: The workflow YAML as a dict (loaded via
            ``yaml.safe_load``). Must have a ``steps:`` block; other
            fields are ignored.
        endpoint: The target endpoint name. Currently accepts any
            string; per-endpoint pricing is not yet implemented.

    Returns:
        ``EstimateCostResponse`` with ``total_core_hours``,
        ``per_step_core_hours``, ``confidence_interval``, ``endpoint``,
        ``novel_python_capped_at=None``.

    Raises:
        ValueError: if ``workflow_config`` lacks a ``steps:`` block
            or ``steps:`` isn't a mapping (YAML format).
    """
    steps = workflow_config.get("steps")
    if not isinstance(steps, dict):
        raise ValueError(
            "estimate_workflow_cost: workflow_config must have a 'steps:' "
            f"mapping; got {type(steps).__name__}"
        )

    per_step: dict[str, float] = {}
    for step_id, step_config in steps.items():
        if not isinstance(step_config, dict):
            # Skip malformed entries — the workflow loader will catch
            # these separately. We don't want the estimator to raise
            # on the same input the framework will catch anyway.
            continue
        per_step[step_id] = _per_step_core_hours(step_id, step_config)

    raw_total = sum(per_step.values())
    total = raw_total * _endpoint_factor(endpoint)

    return EstimateCostResponse(
        total_core_hours=total,
        per_step_core_hours=per_step,
        confidence_interval=(
            total * _CONFIDENCE_LOW_MULTIPLIER,
            total * _CONFIDENCE_HIGH_MULTIPLIER,
        ),
        endpoint=endpoint,
        novel_python_capped_at=None,
    )


def estimated_wall_time_seconds(total_core_hours: float) -> float:
    """Convert core-hours to wall-time seconds under the dumb
    assumption of strictly sequential execution (wall-time = core-hours).

    Actual wall-time with parallelism is ≤ this value. Useful as an
    upper-bound for "will this run before my meeting?" questions.
    """
    return total_core_hours * _SECONDS_PER_HOUR


__all__ = [
    "estimate_workflow_cost",
    "estimated_wall_time_seconds",
]
