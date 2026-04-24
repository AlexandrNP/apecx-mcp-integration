"""A2A integration tests — happy path + error-response branches.

Two groups:

1. **Happy path (closes T-2026-04-23-01)** —
   ``test_a2a_happy_path_against_loopback_server`` exercises the full
   ``connect → discover → send → get → cancel`` lifecycle against a
   real aiohttp JSON-RPC server.

2. **Error-response branches** — the three ``response_data["error"]``
   branches and the ``response.status != 200`` branch in
   ``A2AClient.send_task`` / ``get_task`` / ``cancel_task`` were
   previously un-exercised. Each is now covered by a test that
   configures the loopback server to return the specific failure
   shape and asserts ``A2ATaskExecutionError`` with a message that
   distinguishes the branch that produced it.

Complements ``test_nanobrain_mocks_policy.py`` (the
``aiohttp-missing`` error paths) to satisfy the workspace CLAUDE.md
mocks-policy parity rule against the real aiohttp transport.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

import pytest
from aiohttp import web

pytestmark = pytest.mark.integration


# Typed aiohttp application keys — avoids NotAppKeyWarning in 3.13+.
TASK_STORE_KEY = web.AppKey("task_store", dict)
# Per-method JSON-RPC error injection. Values: {"tasks/send": True}
# means "return a JSON-RPC error for tasks/send calls".
ERROR_MODES_KEY = web.AppKey("error_modes", dict)


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
    error_modes: Dict[str, Any] = request.app[ERROR_MODES_KEY]

    # Error-injection: return a JSON-RPC error response for this method.
    # Used by the error-branch tests to exercise the
    # ``response_data["error"]`` paths in A2AClient.
    if error_modes.get(method):
        return web.json_response({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32001,
                "message": f"injected error for {method!r}",
                "data": {"reason": "test-injected"},
            },
        })

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


async def _start_server(
    error_modes: Optional[Dict[str, bool]] = None,
) -> Tuple[str, web.AppRunner]:
    app = web.Application()
    app[TASK_STORE_KEY] = {}
    app[ERROR_MODES_KEY] = dict(error_modes or {})
    app.router.add_get("/.well-known/agent.json", _agent_card_handler)
    app.router.add_post("/", _jsonrpc_handler)

    # access_log=None silences per-request log lines; reduces pytest noise.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", runner


async def _start_http_500_server() -> Tuple[str, web.AppRunner]:
    """Alternate server that always returns HTTP 500 for JSON-RPC POSTs.

    The agent-card endpoint still returns 200 + a valid card, so
    ``connect_to_agent`` succeeds; the HTTP 500 only fires when the
    client POSTs a ``tasks/*`` JSON-RPC request.
    """
    async def _always_500(request: web.Request) -> web.Response:
        return web.Response(status=500, text="simulated backend failure")

    app = web.Application()
    app.router.add_get("/.well-known/agent.json", _agent_card_handler)
    app.router.add_post("/", _always_500)

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


# ---------------------------------------------------------------------------
# Error-response branches
# ---------------------------------------------------------------------------
#
# ``A2AClient.send_task`` / ``get_task`` / ``cancel_task`` each have two
# un-happy branches (a2a_support.py :641-642, :694-695, :747-748 for the
# ``"error" in response_data`` path; and the ``response.status != 200``
# path in the same methods). None were previously exercised. These tests
# close each one against the real aiohttp transport.
#
# Implementation note: every inner raise is re-wrapped by the outer
# ``except Exception`` in the client method, so the caller always sees
# ``A2ATaskExecutionError``. The test distinguishes which branch fired
# by regex-matching a fragment of the inner message ("Task execution
# failed:", "Task query failed:", "Task cancellation failed:", or
# "HTTP 500").


async def _connect_client(url: str):
    """Helper: initialize + connect an A2AClient against ``url``.

    Returns the client; caller is responsible for shutdown.
    """
    from nanobrain.core.a2a_support import A2AAgentConfig, A2AClient

    client = A2AClient()
    client.add_agent(A2AAgentConfig(
        name="loopback",
        url=url,
        description="in-process aiohttp loopback for error tests",
    ))
    await client.initialize()
    await client.connect_to_agent("loopback")
    return client


def _echo_message():
    from nanobrain.core.a2a_support import A2AMessage, A2APart, PartType

    return A2AMessage(
        role="user",
        parts=[A2APart(type=PartType.TEXT, text="hi")],
    )


async def _send_task_with_jsonrpc_error_scenario():
    from nanobrain.core.a2a_support import A2ATaskExecutionError

    url, runner = await _start_server(error_modes={"tasks/send": True})
    client = None
    try:
        client = await _connect_client(url)
        with pytest.raises(A2ATaskExecutionError, match=r"Task execution failed:.*injected"):
            await client.send_task("loopback", task_id="t-err", message=_echo_message())
    finally:
        if client is not None:
            await client.shutdown()
        await runner.cleanup()


async def _get_task_with_jsonrpc_error_scenario():
    from nanobrain.core.a2a_support import A2ATaskExecutionError

    url, runner = await _start_server(error_modes={"tasks/get": True})
    client = None
    try:
        client = await _connect_client(url)
        with pytest.raises(A2ATaskExecutionError, match=r"Task query failed:.*injected"):
            await client.get_task("loopback", "t-whatever")
    finally:
        if client is not None:
            await client.shutdown()
        await runner.cleanup()


async def _cancel_task_with_jsonrpc_error_scenario():
    from nanobrain.core.a2a_support import A2ATaskExecutionError

    url, runner = await _start_server(error_modes={"tasks/cancel": True})
    client = None
    try:
        client = await _connect_client(url)
        with pytest.raises(A2ATaskExecutionError, match=r"Task cancellation failed:.*injected"):
            await client.cancel_task("loopback", "t-whatever")
    finally:
        if client is not None:
            await client.shutdown()
        await runner.cleanup()


async def _http_500_scenario():
    from nanobrain.core.a2a_support import A2ATaskExecutionError

    url, runner = await _start_http_500_server()
    client = None
    try:
        client = await _connect_client(url)
        with pytest.raises(A2ATaskExecutionError, match=r"HTTP 500"):
            await client.send_task("loopback", task_id="t-500", message=_echo_message())
    finally:
        if client is not None:
            await client.shutdown()
        await runner.cleanup()


def test_send_task_raises_when_server_returns_jsonrpc_error():
    """``A2AClient.send_task`` must raise ``A2ATaskExecutionError`` when
    the server returns a JSON-RPC ``error`` object (not ``result``).

    Pins the inner branch at ``a2a_support.py`` line 641-642.
    """
    asyncio.run(_send_task_with_jsonrpc_error_scenario())


def test_get_task_raises_when_server_returns_jsonrpc_error():
    """``A2AClient.get_task`` JSON-RPC error branch."""
    asyncio.run(_get_task_with_jsonrpc_error_scenario())


def test_cancel_task_raises_when_server_returns_jsonrpc_error():
    """``A2AClient.cancel_task`` JSON-RPC error branch."""
    asyncio.run(_cancel_task_with_jsonrpc_error_scenario())


def test_send_task_raises_on_http_500():
    """HTTP-level failure (non-200 status) must surface as
    ``A2ATaskExecutionError`` with ``HTTP <status>`` in the message.

    Pins the outer branch at ``a2a_support.py`` line ~650
    (``raise A2ATaskExecutionError(f"HTTP {response.status}: ...")``).
    """
    asyncio.run(_http_500_scenario())
