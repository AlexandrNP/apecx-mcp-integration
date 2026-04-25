"""MCP tools for the optional HPC-export lane.

Scientists opt into HPC by calling these tools in sequence:
    estimate_cost(run_id) → confirm_allocation(run_id, core_hours)
        → export_hpc_bundle(run_id, target_system, output_directory)
        → [scientist runs qsub manually on HPC; transfers result back]
    → ingest_hpc_bundle(bundle_path)

``/hpc/submit`` is intentionally not exposed — that would need a live
HPC executor which remains 501 at the Control Plane layer.
"""

from __future__ import annotations

from apecx_integration.control_plane.schemas.api import (
    ConfirmAllocationRequest,
    EstimateCostRequest,
    ExportHpcBundleRequest,
    IngestHpcBundleRequest,
)
from apecx_integration.mcp_surface.tools._shared import (
    get_client,
    parse_run_id,
)


async def estimate_cost(run_id: str) -> dict:
    body = EstimateCostRequest(run_id=parse_run_id(run_id))
    client = get_client()
    result = await client.estimate_cost(body)
    return result.model_dump(mode="json")


async def confirm_allocation(
    run_id: str, confirmed_core_hours: float
) -> dict:
    body = ConfirmAllocationRequest(
        run_id=parse_run_id(run_id),
        confirmed_core_hours=confirmed_core_hours,
    )
    client = get_client()
    result = await client.confirm_allocation(body)
    return result.model_dump(mode="json")


async def export_hpc_bundle(
    run_id: str, target_system: str, output_directory: str
) -> dict:
    body = ExportHpcBundleRequest(
        run_id=parse_run_id(run_id),
        target_system=target_system,
        output_directory=output_directory,
    )
    client = get_client()
    result = await client.export_hpc_bundle(body)
    return result.model_dump(mode="json")


async def ingest_hpc_bundle(bundle_path: str) -> dict:
    body = IngestHpcBundleRequest(bundle_path=bundle_path)
    client = get_client()
    result = await client.ingest_hpc_bundle(body)
    return result.model_dump(mode="json")


__all__ = [
    "confirm_allocation",
    "estimate_cost",
    "export_hpc_bundle",
    "ingest_hpc_bundle",
]
