"""CollectToolsStep — first stage of the pre-warm workflow.

Reads the workflow catalog YAML (defaulting to the packaged
``mcp_workflow_catalog.yml`` or ``$APECX_MCP_WORKFLOW_CATALOG``),
walks every :class:`WorkflowCatalogEntry`, and unions the
``prewarm_rhea_tools`` lists into a deduplicated, order-preserved
list of Rhea-side tool names. Passes through the install-side
config (database_url, redis_host, redis_port, rhea_python) so the
downstream :class:`InstallToolsStep` has everything it needs.

Contract
--------
Input data unit ``prewarm_request`` (dict)::

    {
        "catalog_path": str | None,      # path to YAML; None → packaged default
        "database_url": str,             # Postgres URL for galaxytools table
        "redis_host": str,               # Redis host (orchestrator's cache backend)
        "redis_port": int,               # Redis port
        "rhea_python": str | None,       # path to rhea-venv python binary
    }

Output data unit ``collect_tools_output`` (dict)::

    {
        "tool_names": list[str],         # deduped, order-preserved
        "install_config": {              # pass-through for InstallToolsStep
            "database_url": ...,
            "redis_host": ...,
            "redis_port": ...,
            "rhea_python": ...,
        },
    }

Why pass-through rather than re-derive?
---------------------------------------
The orchestrator already has the authoritative view (its container
specs declare the right host:port for Redis and Postgres). Letting
each step re-derive from env vars would split source-of-truth and
risk config drift. The workflow accepts a single bundled request
dict and threads it through.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

from apecx_integration.infrastructure.rhea_prewarm import (
    _collect_tools_from_catalog,
)
from apecx_integration.mcp_surface.workflow_registry import load_catalog

log = logging.getLogger(__name__)


class CollectToolsStepConfig(StepConfig):
    """Config for :class:`CollectToolsStep`.

    No step-specific fields — the catalog path arrives via the
    ``prewarm_request`` data unit at process time, so the step is
    stateless and reusable across catalogs without re-instantiation.

    ``extra='forbid'`` is set per workspace policy so YAML typos in
    the step config surface as validation errors at load time, not
    silently as defaults.

    ``validate_assignment=False`` (overriding ConfigBase's True
    default) so the framework can attach its own post-instantiation
    attributes like ``source_path`` via ``setattr`` without tripping
    Pydantic's per-field re-validation.
    """

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_assignment=False,
    )

    # Framework-set: ConfigBase.from_config() does
    # ``setattr(config_instance, 'source_path', str(config_path))`` after
    # validation. With ``extra='forbid'`` we must declare it explicitly
    # so the assignment doesn't raise.
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML the config was loaded from.",
    )


class CollectToolsStep(BaseStep):
    """Catalog walker that emits the deduped Rhea tool-name list.

    Reuses :func:`_collect_tools_from_catalog` so this step and the
    legacy ``prewarm_workflow_catalog`` helper are guaranteed to
    produce the same tool set — preventing a "two implementations
    drift apart" silent failure during the refactor.
    """

    COMPONENT_TYPE: str = "collect_tools_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CollectToolsStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        request = input_data.get("prewarm_request")
        if not isinstance(request, dict):
            raise ValueError(
                f"CollectToolsStep {self.name!r}: expected input_data["
                f"'prewarm_request'] to be a dict, got "
                f"{type(request).__name__}. The orchestrator wires the "
                f"workflow's `prewarm_request` data unit to this step's "
                f"input via DirectLink; check the workflow YAML."
            )
        required = {"database_url", "redis_host", "redis_port"}
        missing = required - request.keys()
        if missing:
            raise ValueError(
                f"CollectToolsStep {self.name!r}: prewarm_request is "
                f"missing required keys {sorted(missing)!r}. The "
                f"orchestrator's prewarm_workflow_tools() composes this "
                f"dict from its backend specs; check that postgres + "
                f"redis backends are registered."
            )
        catalog = load_catalog(request.get("catalog_path"))
        tool_names = _collect_tools_from_catalog(catalog)
        log.info(
            "prewarm_workflow.collect_tools: %d tool(s) to pre-warm: %s",
            len(tool_names),
            tool_names,
        )
        install_config = {
            "database_url": request["database_url"],
            "redis_host": request["redis_host"],
            "redis_port": int(request["redis_port"]),
            "rhea_python": request.get("rhea_python"),
        }
        return {
            "collect_tools_output": {
                "tool_names": tool_names,
                "install_config": install_config,
            }
        }
