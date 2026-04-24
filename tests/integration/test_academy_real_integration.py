"""G5 — Academy real-integration test (closes T-2026-04-23-02).

Exercises ``nanobrain.core.academy_integration`` against a real local
Academy agent (``academy.agent.Agent`` subclass) via ``academy-py``.
No mocks — the test **launches** a real Academy agent via
``AcademyManagerWrapper.register_agent_class`` and dispatches actions
through the real ``academy.handle.Handle``.

Satisfies the workspace CLAUDE.md mocks-policy parity rule:
``test_nanobrain_mocks_policy.py`` covers the ``ACADEMY_DEMO_MODE``
fallback + the ``AcademyNotImplementedError`` class; this file covers
the positive real path.

## Why this test exists as a durable artifact

nanobrain is not a git repo on this workspace (friction log #6). If
a re-fetch reverts ``nanobrain/core/academy_integration.py`` to its
pre-G5 state, **every test in this file flips red**, because the
pre-G5 code:

- Does NOT enter the Academy Manager's ``async with`` context
  (calling ``await manager.launch(...)`` would succeed but
  ``shutdown`` would raise ``ExchangeClientNotFoundError`` — the
  real path was never functional).
- Does NOT have ``register_agent_class``.
- ``AcademyAgentHandle.__call__`` raises ``AcademyNotImplementedError``
  in the non-demo path rather than dispatching through the real
  Handle.

## Why not a skipif-gated test

``academy-py`` is now a declared dependency of apecx-mcp-integration
(pyproject.toml ``[project.optional-dependencies].academy`` —
installed automatically in the canonical venv). If it goes missing,
that is a test-environment regression worth seeing as an explicit
failure, not a quiet skip.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# A real Academy agent for the test to launch + invoke.
# Defined at module level so ``manager.launch(Echo)`` can pickle it.
# ---------------------------------------------------------------------------

from academy.agent import Agent, action


class Echo(Agent):
    """Trivial Academy agent — echoes its input, counts invocations."""

    async def agent_on_startup(self) -> None:
        self._calls = 0

    @action
    async def echo(self, msg: str) -> str:
        self._calls += 1
        return f"echo[{self._calls}]: {msg}"

    @action
    async def tally(self) -> int:
        return self._calls


# ---------------------------------------------------------------------------
# Fixtures — each test gets a clean Academy singleton.
# ---------------------------------------------------------------------------


@pytest.fixture
async def academy_manager() -> AsyncIterator:
    """Yield a fresh ``AcademyManagerWrapper`` singleton; tear down after."""
    from nanobrain.core.academy_integration import (
        AcademyIntegration,
        shutdown_academy_manager,
    )

    # Make sure no prior test left state around.
    await shutdown_academy_manager()
    mgr = AcademyIntegration.setup_academy_manager()
    try:
        yield mgr
    finally:
        await shutdown_academy_manager()


@pytest.fixture(autouse=True)
def _force_real_mode(monkeypatch):
    """Default tests in this file to the REAL Academy path.

    Tests that need demo mode opt in explicitly via
    ``monkeypatch.setenv('ACADEMY_DEMO_MODE', '1')``.
    """
    monkeypatch.delenv("ACADEMY_DEMO_MODE", raising=False)


# ---------------------------------------------------------------------------
# Real-path tests
# ---------------------------------------------------------------------------


async def test_register_launches_and_dispatches_real_action(academy_manager):
    """The full happy path: register → await handle.echo(data) → real result.

    Pins that:
    1. ``register_agent_class`` actually launches a real Academy agent.
    2. Dispatch via ``__getattr__`` (``wrapper.echo(...)``) hits the real
       handle, NOT the demo-mode mock.
    3. The real agent's state persists across calls (counter increments).
    """
    handle = await academy_manager.register_agent_class("echo_agent", Echo)

    first = await handle.echo("hello")
    second = await handle.echo("world")

    assert first == "echo[1]: hello"
    assert second == "echo[2]: world"

    # A second action on the same agent — confirms state persistence.
    count = await handle.tally()
    assert count == 2


async def test_dispatch_via_call_syntax(academy_manager):
    """``await wrapper(action_name, *args)`` must also hit the real path.

    Some Nanobrain call sites (mixin-style) invoke via ``__call__`` rather
    than attribute access. This pins that path too.
    """
    handle = await academy_manager.register_agent_class("echo_call", Echo)
    result = await handle("echo", "via-call")
    assert result == "echo[1]: via-call"


async def test_register_agent_class_is_idempotent(academy_manager):
    """Re-registering the same agent name does not re-launch."""
    h1 = await academy_manager.register_agent_class("same_agent", Echo)
    h2 = await academy_manager.register_agent_class("same_agent", Echo)
    assert h1 is h2
    # Counter should be on the same underlying agent.
    await h1.echo("x")
    await h2.echo("y")
    count = await h1.tally()
    assert count == 2


async def test_unregistered_agent_raises_not_implemented(academy_manager):
    """Real-mode + no registration → clear error with migration hint."""
    from nanobrain.core.academy_integration import AcademyNotImplementedError

    placeholder = academy_manager.get_handle("nonexistent_agent")
    with pytest.raises(AcademyNotImplementedError, match="no real Handle registered"):
        await placeholder("echo", "x")


async def test_unknown_action_on_real_agent_raises(academy_manager):
    """A real agent that doesn't define an action must surface a clear
    error (not a silent mock response) when that action is invoked.

    Academy's own runtime raises ``AttributeError`` with the message
    ``Agent<ClassName> does not have an action named 'action_name'``.
    The wrapper deliberately does NOT re-type this — a real bug
    inside a legitimate action also raises AttributeError, and
    converting it would mislabel implementation bugs as
    "integration incomplete".
    """
    handle = await academy_manager.register_agent_class("echo_unknown", Echo)
    with pytest.raises(
        AttributeError,
        match=r"does not have an action named .nonexistent_action.",
    ):
        await handle("nonexistent_action", "x")


# ---------------------------------------------------------------------------
# Demo-mode regression guard — must NOT be broken by the G5 fix.
# ---------------------------------------------------------------------------


async def test_demo_mode_still_produces_mock_response(academy_manager, monkeypatch):
    """``ACADEMY_DEMO_MODE=1`` must still return the aurora-demo mock
    for ``aurora_computation_agent.process``, even WITHOUT registering
    a real handle. This preserves pre-G5 aurora demo behavior.
    """
    monkeypatch.setenv("ACADEMY_DEMO_MODE", "1")

    placeholder = academy_manager.get_handle("aurora_computation_agent")
    result = await placeholder("process", {"prepared_sequences": ["s1", "s2"]})

    assert result == {"prepared_sequences": ["s1", "s2"]}
