"""MCP tools for the HITL approval lifecycle.

The scientist uses these to respond to PAUSED runs — the composer
may have produced novel Python that needs review, or a nanobrain
ApprovalStep may have paused mid-execution.
"""

from __future__ import annotations

from apecx_integration.control_plane.schemas.api import (
    ApproveRequest,
    CorrectRequest,
    ListPendingApprovalsRequest,
    RejectRequest,
)
from apecx_integration.mcp_surface.tools._shared import (
    get_client,
    parse_run_id,
)


async def list_pending_approvals(user_id: str) -> dict:
    """List pending approvals for a given ``user_id``.

    The MCP surface requires an explicit user to scope — the HITL
    queue is per-scientist, not global.
    """
    body = ListPendingApprovalsRequest(user_id=user_id)
    client = get_client()
    result = await client.list_pending_approvals(body)
    return result.model_dump(mode="json")


async def approve(
    approval_id: str, comment: str = "", decided_by: str = "api_user"
) -> dict:
    body = ApproveRequest(
        approval_id=parse_run_id(approval_id, field="approval_id"),
        comment=comment,
        decided_by=decided_by,
    )
    client = get_client()
    result = await client.approve(body)
    return result.model_dump(mode="json")


async def reject(
    approval_id: str, reason: str, decided_by: str = "api_user"
) -> dict:
    """Reject a pending approval. ``reason`` is required (min length 1)
    by ``RejectRequest`` — a reviewer who rejects must justify it."""
    body = RejectRequest(
        approval_id=parse_run_id(approval_id, field="approval_id"),
        reason=reason,
        decided_by=decided_by,
    )
    client = get_client()
    result = await client.reject(body)
    return result.model_dump(mode="json")


async def correct(
    approval_id: str,
    modifications: dict,
    decided_by: str = "api_user",
) -> dict:
    """Approve with reviewer-supplied modifications. ``modifications``
    is the free-form payload the reviewer wants applied to the
    proposed action; downstream consumers interpret its shape."""
    body = CorrectRequest(
        approval_id=parse_run_id(approval_id, field="approval_id"),
        modifications=modifications,
        decided_by=decided_by,
    )
    client = get_client()
    result = await client.correct(body)
    return result.model_dump(mode="json")


__all__ = [
    "approve",
    "correct",
    "list_pending_approvals",
    "reject",
]
