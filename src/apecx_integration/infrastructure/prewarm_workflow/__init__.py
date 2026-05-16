"""Rhea tool-env pre-warm phase, expressed as a nanobrain workflow.

This package replaces the imperative ``infrastructure/rhea_prewarm.py``
pipeline (still kept as the underlying "atom" helpers) with a real
nanobrain :class:`Workflow` whose three steps are wired by
``DirectLink`` (all ``auto_transfer: true``):

    prewarm_request
         │
         ▼
    collect_tools          (CollectToolsStep)
         │ {tool_names, config}
         ▼
    install_tools          (InstallToolsStep)
         │ {results: list[ToolPrewarmResult]}
         ▼
    aggregate_report       (AggregateReportStep)
         │ {prewarm_report: PrewarmReport}
         ▼
    prewarm_report (workflow output)

Why a workflow and not a function call
--------------------------------------
The old ``prewarm_workflow_catalog(...)`` function works correctly,
but it's an imperative driver outside the nanobrain framework. The
workflow form:

* makes the pipeline visible in nanobrain's DAG (operators can see the
  three stages as named, configurable steps);
* lets future extensions (parallelism via ``ParallelStep``, retries via
  ``LoopController``, per-tool gating via ``ConditionalLink``) be
  expressed with first-class nanobrain primitives rather than ad-hoc
  Python branches;
* allows the orchestrator to drive pre-warm the same way it drives
  every other apecx workflow — ``Workflow.from_config(...)`` +
  ``process()`` + ``wait_for_cascade()``.

Two authoring paths co-exist (per the workspace "multiple legit ways
of creating workflows" guidance):

* :mod:`.configs.prewarm_workflow` — the YAML + :class:`Workflow.from_config`
  path (canonical, reviewable, checked into the repo).
* :mod:`.builder` — a :class:`nanobrain.lightweight.WorkflowBuilder`
  programmatic variant that constructs the same DAG in Python. Useful
  when the workflow is composed at runtime (e.g., from an operator-
  generated catalog override) and proof that the workflow is not YAML-
  load-bearing.
"""

from .aggregate_report_step import AggregateReportStep, AggregateReportStepConfig
from .collect_tools_step import CollectToolsStep, CollectToolsStepConfig
from .install_tools_step import InstallToolsStep, InstallToolsStepConfig

__all__ = [
    "AggregateReportStep",
    "AggregateReportStepConfig",
    "CollectToolsStep",
    "CollectToolsStepConfig",
    "InstallToolsStep",
    "InstallToolsStepConfig",
]
