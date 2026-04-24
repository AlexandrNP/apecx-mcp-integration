"""A2A happy-path integration test — closes T-2026-04-23-01.

Exercises the full ``connect → discover → send → get → cancel``
lifecycle of ``nanobrain.core.a2a_support.A2AClient`` against a real
in-process ``aiohttp`` JSON-RPC server. No mocks, no monkeypatching
— real transport, real JSON-RPC framing, real HTTP.

Complements ``test_nanobrain_mocks_policy.py`` (error paths only) to
satisfy the workspace ``CLAUDE.md`` mocks-policy parity rule: every
behavior a unit test covers via mocks must have a corresponding
integration test against a real backend. Error paths live in the
mocks-policy file; this file is the positive-path counterpart.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

import pytest
from aiohttp import web

pytestmark = pytest.mark.integration


# Typed aiohttp application key — avoids NotAppKeyWarning in 3.13+.
TASK_STORE_KEY = web.AppKey("task_store", dict)


AGENT_CARD_TEMPLATE: Dict[str, Any] = {
    "name": "test-agent",
    "description": "Loopback A2A agent for integration testing",
    "version": "1.0.0",
    "provider": {
        "organization": "apecx-integration-tests",
        "contact": None,
        "website": None,
    },
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "authentication": {"schemes": ["none"]},
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
    "skills": [
        {
            "id": "echo",
            "name": "Echo",
            "description": "Echoes user text back in a completion artifact.",
            "tags": ["test", "echo"],
            "examples": ["Hello there"],
            "inputModes": ["text"],
            "outputModes": ["text"],
        }
    ],
}


def _completed_task(
    task_id: str, session_id: Optional[str], echo_text: str
) -> Dict[str, Any]:
    return {
        "id": task_id,
        "status": {
            "state": "completed",
            "message": {
                "role": "agent",
                "parts": [{"type": "text", "text": "done"}],
            },
        },
        "sessionId": session_id,
        "artifacts": [
            {
                "name": "echo",
                "description": "Echo response",
                "parts": [{"type": "text", "text": f"echo: {echo_text}"}],
                "index": 0,
                "append": False,
                "lastChunk": True,
            }
        ],
        "history": [],
        "metadata": None,
    }


def _canceled_task(task_id: str) -> Dict[str, Any]:
    return {
        "id": task_id,
        "status": {
            "state": "canceled",
            "message": {
                "role": "agent",
                "parts": [{"type": "text", "text": "task canceled"}],
            },
        },
        "sessionId": None,
        "artifacts": [],
        "history": [],
        "metadata": None,
    }


async def _agent_card_handler(request: web.Request) -> web.Response:
    url = str(request.url.origin())
    card = dict(AGENT_CARD_TEMPLATE)
    card["url"] = url
    return web.json_response(card)


async def _jsonrpc_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    method = payload.get("method")
    params = payload.get("params", {})
    rpc_id = payload.get("id")

    store: Dict[str, Dict[str, Any]] = request.app[TASK_STORE_KEY]

    if method == "tasks/send":
        task_id = params["id"]
        session_id = params.get("sessionId")
        message = params.get("message", {})
        echo_text = ""
        for part in message.get("parts", []):
            if part.get("type") == "text" and part.get("text"):
                echo_text = part["text"]
                break
        task = _completed_task(task_id, session_id, echo_text)
        store[task_id] = task
        return web.json_response({"jsonrpc": "2.0", "id": rpc_id, "result": task})

    if method == "tasks/get":
        task_id = params["id"]
        if task_id not in store:
            return web.json_response({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32000, "message": "task not found"},
            })
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": store[task_id]}
        )

    if method == "tasks/cancel":
        task_id = params["id"]
        canceled = _canceled_task(task_id)
        store[task_id] = canceled
        return web.json_response(
            {"jsonrpc": "2.0", "id": rpc_id, "result": canceled}
        )

    return web.json_response({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"unknown method {method!r}"},
    })


async def _start_server() -> Tuple[str, web.AppRunner]:
    app = web.Application()
    app[TASK_STORE_KEY] = {}
    app.router.add_get("/.well-known/agent.json", _agent_card_handler)
    app.router.add_post("/", _jsonrpc_handler)

    # access_log=None silences per-request log lines; reduces pytest noise.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", runner


async def _scenario() -> None:
    from nanobrain.core.a2a_support import (
        A2AAgentConfig,
        A2AClient,
        A2AMessage,
        A2APart,
        PartType,
        TaskStatus,
    )

    url, runner = await _start_server()
    client: Optional[A2AClient] = None
    try:
        client = A2AClient()
        client.add_agent(A2AAgentConfig(
            name="loopback",
            url=url,
            description="in-process aiohttp loopback",
        ))
        await client.initialize()

        connected = await client.connect_to_agent("loopback")
        assert connected is True

        card = await client.discover_agent_capabilities("loopback")
        assert card.name == "test-agent"
        assert len(card.skills) == 1
        assert card.skills[0].id == "echo"
        assert card.url == url

        msg = A2AMessage(
            role="user",
            parts=[A2APart(type=PartType.TEXT, text="hello")],
        )
        sent = await client.send_task(
            "loopback", task_id="t-1", message=msg, session_id="s-1"
        )
        assert sent.id == "t-1"
        assert sent.status.state == TaskStatus.COMPLETED
        assert sent.sessionId == "s-1"
        assert sent.artifacts is not None
        assert len(sent.artifacts) == 1
        assert sent.artifacts[0].parts[0].text == "echo: hello"

        fetched = await client.get_task("loopback", "t-1")
        assert fetched.id == "t-1"
        assert fetched.status.state == TaskStatus.COMPLETED
        assert fetched.artifacts[0].parts[0].text == "echo: hello"

        canceled = await client.cancel_task("loopback", "t-1")
        assert canceled.id == "t-1"
        assert canceled.status.state == TaskStatus.CANCELED
    finally:
        if client is not None:
            await client.shutdown()
        await runner.cleanup()


def test_a2a_happy_path_against_loopback_server():
    """T-2026-04-23-01: full connect → discover → send → get → cancel
    against a real in-process aiohttp JSON-RPC server.

    Closes the mocks-policy parity gap for ``a2a_support.py``: error
    paths are covered by
    ``tests/integration/test_nanobrain_mocks_policy.py``; this test
    covers the positive paths end-to-end against real aiohttp
    transport — no mocks, no monkeypatching, no in-memory shims.
    """
    asyncio.run(_scenario())
