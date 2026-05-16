"""InstallToolsStep — second stage of the pre-warm workflow.

Drives the actual per-tool install path. For each tool the upstream
:class:`CollectToolsStep` emitted, runs :func:`prewarm_tool` (which
does cache-probe → fetch requirements from Postgres → spawn rhea-venv
subprocess → install_conda_env). Each tool yields a
:class:`ToolPrewarmResult`; the full list is emitted on
``install_tools_output``.

Contract
--------
Input data unit ``collect_tools_output`` (dict)::

    {
        "tool_names": list[str],
        "install_config": {
            "database_url": str,
            "redis_host": str,
            "redis_port": int,
            "rhea_python": str | None,
        },
    }

Output data unit ``install_tools_output`` (dict)::

    {
        "results": list[ToolPrewarmResult],   # one per tool, serial walk
    }

Serial-only on purpose
----------------------
``conda``'s package cache and prefix lock are not safe under
concurrent installs in the same prefix. Even if we wanted to
parallelize across tools, two simultaneous ``conda create -n A`` and
``conda create -n B`` calls can corrupt each other's metadata when
they both touch the package cache. The serial walk is correct;
parallelism is a future ``ParallelStep`` exercise that would need a
per-tool isolated conda prefix to be safe.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

from apecx_integration.infrastructure.rhea_prewarm import (
    ToolPrewarmResult,
    prewarm_tool,
)

log = logging.getLogger(__name__)


class InstallToolsStepConfig(StepConfig):
    """Config for :class:`InstallToolsStep`.

    Stateless step (no step-specific fields). The install_config
    arrives via the upstream data unit at process time, so a single
    step instance can serve any catalog without reconfiguration.

    See :class:`CollectToolsStepConfig` for the rationale behind
    ``extra='forbid'`` + ``validate_assignment=False``.
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

    # Read by the framework via getattr at execute() time (see
    # nanobrain.core.step.py:1569). Default 300s is too tight for a
    # multi-tool serial install where each fresh conda env takes
    # 30-90s. The catalog as it stands declares only `muscle`; bumping
    # to 1800s gives headroom for ~20 tools at the upper end before
    # the per-step timeout bites. The pre-warm phase is fail-loud at
    # any timeout (the actor wedge problem this whole pipeline exists
    # to prevent), so a generous ceiling is correct.
    execution_timeout: float = Field(
        default=1800.0,
        description="Per-step execution timeout in seconds.",
    )


class InstallToolsStep(BaseStep):
    """Per-tool serial installer using :func:`prewarm_tool`."""

    COMPONENT_TYPE: str = "install_tools_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return InstallToolsStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        payload = input_data.get("collect_tools_output")
        if not isinstance(payload, dict):
            raise ValueError(
                f"InstallToolsStep {self.name!r}: expected input_data["
                f"'collect_tools_output'] to be a dict (the upstream "
                f"CollectToolsStep's emit), got {type(payload).__name__}."
            )
        tool_names = payload.get("tool_names", [])
        install_config = payload.get("install_config") or {}
        if not isinstance(tool_names, list):
            raise ValueError(
                f"InstallToolsStep {self.name!r}: tool_names must be a list, "
                f"got {type(tool_names).__name__}."
            )

        results: list[ToolPrewarmResult] = []
        for tool in tool_names:
            result = await prewarm_tool(
                tool,
                database_url=install_config["database_url"],
                redis_host=install_config["redis_host"],
                redis_port=int(install_config["redis_port"]),
                rhea_python=install_config.get("rhea_python"),
            )
            results.append(result)
            if result.state == "failed":
                log.error(
                    "prewarm_workflow.install_tools: %r FAILED in %.1fs: %s",
                    tool,
                    result.latency_seconds,
                    result.detail,
                )
            else:
                log.info(
                    "prewarm_workflow.install_tools: %r %s in %.1fs",
                    tool,
                    result.state,
                    result.latency_seconds,
                )
        return {"install_tools_output": {"results": results}}
