"""MCP tools for the scientist-facing workflow lifecycle.

Each tool is a thin wrapper over ``ControlPlaneClient`` that marshals
MCP-friendly input types (strs) into the Control Plane's request
envelopes and returns plain dicts / strings MCP can serialize.

Names follow AP §3 — these are what Claude Desktop surfaces as tools.
"""

from __future__ import annotations

from uuid import UUID

from apecx_integration.control_plane.schemas.api import (
    ExecuteWorkflowRequest,
    ShowYamlDiffRequest,
    StartWorkflowRequest,
)
from apecx_integration.control_plane.schemas.enums import ExecutorKind
from apecx_integration.mcp_surface.tools._shared import get_client


async def start_workflow(
    description: str,
    user_id: str,
    preferred_executor: str = "local",
) -> dict:
    """Compose a workflow from a natural-language description.

    Returns the newly-created Run (with status PAUSED or RUNNING
    depending on the approval policy) and the generated workflow
    artifact id.
    """
    body = StartWorkflowRequest(
        description=description,
        user_id=user_id,
        preferred_executor=ExecutorKind(preferred_executor),
    )
    client = get_client()
    result = await client.start_workflow(body)
    return result.model_dump(mode="json")


async def show_diff(run_id: str) -> dict:
    """Surface the T06 differential-review payload for a run.

    Returns ``yaml_text``, ``novel_python_by_step``, per-step
    ``categorization`` (composed_standard / composed_parameterized /
    composed_wrapped / novel), and a one-sentence summary.
    """
    body = ShowYamlDiffRequest(run_id=UUID(run_id))
    client = get_client()
    result = await client.show_yaml_diff(body)
    return result.model_dump(mode="json")


async def execute_workflow(run_id: str) -> dict:
    """Run the composed workflow locally.

    Synchronous wrt MCP — holds the tool call until the LocalExecutor
    reaches terminal state. Returns ``status`` (completed / failed),
    ``output_artifact_id`` (on success), and ``reason`` (on failure).
    """
    body = ExecuteWorkflowRequest(run_id=UUID(run_id))
    client = get_client()
    result = await client.execute_workflow(body)
    return result.model_dump(mode="json")


__all__ = ["execute_workflow", "show_diff", "start_workflow"]
