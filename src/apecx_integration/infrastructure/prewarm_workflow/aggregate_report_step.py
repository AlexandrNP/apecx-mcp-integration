"""AggregateReportStep — final stage of the pre-warm workflow.

Consumes the per-tool results list emitted by
:class:`InstallToolsStep` and produces a fully-built
:class:`PrewarmReport`, stamping ``started_at`` / ``completed_at`` and
exposing the :meth:`PrewarmReport.snapshot` shape via the workflow
output data unit.

Contract
--------
Input data unit ``install_tools_output`` (dict)::

    {"results": list[ToolPrewarmResult]}

Output data unit ``prewarm_report`` (PrewarmReport)::

    PrewarmReport(
        tools=[ToolPrewarmResult, ...],
        started_at=<earliest tool latency anchor>,
        completed_at=<time of aggregation>,
    )

Why return the dataclass, not its snapshot dict?
------------------------------------------------
The orchestrator's status() code reads ``report.snapshot()`` AND
iterates ``report.tools`` to lift failures into the ``actionable``
list. Returning the dataclass keeps that contract intact. The
workflow's output data unit (DataUnitMemory) stores arbitrary Python,
so passing the dataclass through is the simplest framework-native
shape — no JSON round-trip, no shape conversion.

Why this is a separate step instead of part of InstallToolsStep
---------------------------------------------------------------
Separation of concerns: install_tools does I/O against conda + Redis;
aggregate_report is a pure data transformation (list of results →
typed report). Splitting them makes each step trivially unit-testable
in isolation: install can be exercised against a fake catalog,
aggregate can be exercised against a fixed result list. The cost is
one extra data unit and one extra link; the benefit is that any
future schema evolution on the report (e.g., add a "duration_total",
add a "fastest/slowest tool" summary) localizes to this one step
without touching the install path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

from apecx_integration.infrastructure.rhea_prewarm import (
    PrewarmReport,
    ToolPrewarmResult,
)

log = logging.getLogger(__name__)


class AggregateReportStepConfig(StepConfig):
    """Config for :class:`AggregateReportStep`.

    Stateless step. See :class:`CollectToolsStepConfig` for the
    rationale behind ``extra='forbid'`` + ``validate_assignment=False``.
    """

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_assignment=False,
    )

    # Framework-set: see CollectToolsStepConfig.source_path for rationale.
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML the config was loaded from.",
    )


class AggregateReportStep(BaseStep):
    """Builds the final :class:`PrewarmReport` from per-tool results."""

    COMPONENT_TYPE: str = "aggregate_report_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return AggregateReportStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        payload = input_data.get("install_tools_output")
        if not isinstance(payload, dict):
            raise ValueError(
                f"AggregateReportStep {self.name!r}: expected input_data["
                f"'install_tools_output'] to be a dict, got "
                f"{type(payload).__name__}."
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError(
                f"AggregateReportStep {self.name!r}: results must be a list, "
                f"got {type(results).__name__}."
            )
        # Every element should be a ToolPrewarmResult. Fail loudly on
        # shape drift — silently coercing strings or dicts here would
        # break the status tool's iteration.
        for i, r in enumerate(results):
            if not isinstance(r, ToolPrewarmResult):
                raise ValueError(
                    f"AggregateReportStep {self.name!r}: results[{i}] is "
                    f"{type(r).__name__}, expected ToolPrewarmResult. "
                    f"The InstallToolsStep contract emits dataclass "
                    f"instances; a dict here indicates a serialization "
                    f"round-trip that flattened the type."
                )

        now = time.time()
        # If we have any tools, anchor started_at to the earliest one's
        # latency window. Otherwise mark it as now (empty pre-warm).
        started_at = (
            now - max((r.latency_seconds for r in results), default=0.0) if results else now
        )
        report = PrewarmReport(
            tools=results,
            started_at=started_at,
            completed_at=now,
        )
        log.info(
            "prewarm_workflow.aggregate_report: %d tool(s), all_ready=%s",
            len(results),
            report.all_ready,
        )
        return {"prewarm_report": report}
